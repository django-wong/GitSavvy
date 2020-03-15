"""
Implements a special view to visualize and stage pieces of a project's
current diff.
"""

from collections import namedtuple
from contextlib import contextmanager
import os
import re

import sublime
from sublime_plugin import WindowCommand, TextCommand, EventListener

from . import intra_line_colorizer
from .navigate import GsNavigate
from ..fns import filter_, flatten
from ..parse_diff import SplittedDiff
from ..runtime import enqueue_on_ui, enqueue_on_worker
from ..utils import line_indentation
from ..git_command import GitCommand
from ..exceptions import GitSavvyError
from ...common import util


MYPY = False
if MYPY:
    from typing import (
        Iterable, Iterator, List, NamedTuple, Optional, Set,
        Tuple, TypeVar
    )
    from ..parse_diff import Hunk, HunkLine

    T = TypeVar('T')
    Point = int
    RowCol = Tuple[int, int]
    HunkLineWithB = NamedTuple('HunkLineWithB', [('line', 'HunkLine'), ('b', int)])
else:
    HunkLineWithB = namedtuple('HunkLineWithB', 'line b')


DIFF_TITLE = "DIFF: {}"
DIFF_CACHED_TITLE = "DIFF: {} (staged)"

# Clickable lines:
# (A)  common/commands/view_manipulation.py  |   1 +
# (B) --- a/common/commands/view_manipulation.py
# (C) +++ b/common/commands/view_manipulation.py
# (D) diff --git a/common/commands/view_manipulation.py b/common/commands/view_manipulation.py
FILE_RE = (
    r"^(?:\s(?=.*\s+\|\s+\d+\s)|--- a\/|\+{3} b\/|diff .+b\/)"
    #     ^^^^^^^^^^^^^^^^^^^^^ (A)
    #     ^ one space, and then somewhere later on the line the pattern `  |  23 `
    #                           ^^^^^^^ (B)
    #                                   ^^^^^^^^ (C)
    #                                            ^^^^^^^^^^^ (D)
    r"(\S[^|]*?)"
    #         ^ ! lazy to not match the trailing spaces, see below

    r"(?:\s+\||$)"
    #          ^ (B), (C), (D)
    #    ^^^^^ (A) We must match the spaces here bc Sublime will not rstrip() the
    #    filename for us.
)

# Clickable line:
# @@ -69,6 +69,7 @@ class GsHandleVintageousCommand(TextCommand):
#           ^^ we want the second (current) line offset of the diff
LINE_RE = r"^@@ [^+]*\+(\d+)"


def compute_identifier_for_view(view):
    # type: (sublime.View) -> Optional[Tuple]
    settings = view.settings()
    return (
        settings.get('git_savvy.repo_path'),
        settings.get('git_savvy.file_path'),
        settings.get('git_savvy.diff_view.base_commit'),
        settings.get('git_savvy.diff_view.target_commit')
    ) if settings.get('git_savvy.diff_view') else None


def focus_view(view):
    window = view.window()
    if not window:
        return

    group, _ = window.get_view_index(view)
    window.focus_group(group)
    window.focus_view(view)


class GsDiffCommand(WindowCommand, GitCommand):

    """
    Create a new view to display the difference of `target_commit`
    against `base_commit`. If `target_commit` is None, compare
    working directory with `base_commit`.  If `in_cached_mode` is set,
    display a diff of the Git index. Set `disable_stage` to True to
    disable Ctrl-Enter in the diff view.
    """

    def run(
        self,
        repo_path=None,
        file_path=None,
        in_cached_mode=False,
        current_file=False,
        base_commit=None,
        target_commit=None,
        disable_stage=False,
        title=None,
        ignore_whitespace=False,
        show_word_diff=False,
        context_lines=3
    ):
        if repo_path is None:
            repo_path = self.repo_path
        assert repo_path
        if current_file:
            file_path = self.file_path or file_path

        this_id = (
            repo_path,
            file_path,
            base_commit,
            target_commit
        )
        for view in self.window.views():
            if compute_identifier_for_view(view) == this_id:
                settings = view.settings()
                focus_view(view)
                break

        else:
            diff_view = util.view.get_scratch_view(self, "diff", read_only=True)

            show_diffstat = self.savvy_settings.get("show_diffstat", True)
            settings = diff_view.settings()
            settings.set("git_savvy.repo_path", repo_path)
            settings.set("git_savvy.file_path", file_path)
            settings.set("git_savvy.diff_view.in_cached_mode", in_cached_mode)
            settings.set("git_savvy.diff_view.ignore_whitespace", ignore_whitespace)
            settings.set("git_savvy.diff_view.show_word_diff", show_word_diff)
            settings.set("git_savvy.diff_view.context_lines", context_lines)
            settings.set("git_savvy.diff_view.base_commit", base_commit)
            settings.set("git_savvy.diff_view.target_commit", target_commit)
            settings.set("git_savvy.diff_view.show_diffstat", show_diffstat)
            settings.set("git_savvy.diff_view.disable_stage", disable_stage)
            settings.set("git_savvy.diff_view.history", [])
            settings.set("git_savvy.diff_view.just_hunked", "")

            settings.set("result_file_regex", FILE_RE)
            settings.set("result_line_regex", LINE_RE)
            settings.set("result_base_dir", repo_path)

            if not title:
                title = (DIFF_CACHED_TITLE if in_cached_mode else DIFF_TITLE).format(
                    os.path.basename(file_path) if file_path else os.path.basename(repo_path)
                )
            diff_view.set_name(title)
            diff_view.set_syntax_file("Packages/GitSavvy/syntax/diff_view.sublime-syntax")

            diff_view.run_command("gs_handle_vintageous")


WORD_DIFF_PATTERNS = [
    None,
    r"[a-zA-Z_\-\x80-\xff]+|[^[:space:]]|[\xc0-\xff][\x80-\xbf]+",
    ".",
]
WORD_DIFF_MARKERS_RE = re.compile(r"{\+(.*?)\+}|\[-(.*?)-\]")


class GsDiffRefreshCommand(TextCommand, GitCommand):
    """Refresh the diff view with the latest repo state."""

    def run(self, edit, sync=True):
        if sync:
            self.run_impl(sync)
        else:
            enqueue_on_worker(self.run_impl, sync)

    def run_impl(self, runs_on_ui_thread):
        if self.view.settings().get("git_savvy.disable_diff"):
            return
        repo_path = self.view.settings().get("git_savvy.repo_path")
        file_path = self.view.settings().get("git_savvy.file_path")
        in_cached_mode = self.view.settings().get("git_savvy.diff_view.in_cached_mode")
        ignore_whitespace = self.view.settings().get("git_savvy.diff_view.ignore_whitespace")
        show_word_diff = self.view.settings().get("git_savvy.diff_view.show_word_diff")
        base_commit = self.view.settings().get("git_savvy.diff_view.base_commit")
        target_commit = self.view.settings().get("git_savvy.diff_view.target_commit")
        show_diffstat = self.view.settings().get("git_savvy.diff_view.show_diffstat")
        disable_stage = self.view.settings().get("git_savvy.diff_view.disable_stage")
        context_lines = self.view.settings().get('git_savvy.diff_view.context_lines')

        word_diff_regex = WORD_DIFF_PATTERNS[show_word_diff]

        prelude = "\n"
        title = ["DIFF:"]
        if file_path:
            rel_file_path = os.path.relpath(file_path, repo_path)
            prelude += "  FILE: {}\n".format(rel_file_path)
            title += [os.path.basename(file_path)]
        elif not disable_stage:
            title += [os.path.basename(repo_path)]

        if disable_stage:
            if in_cached_mode:
                prelude += "  {}..INDEX\n".format(base_commit or target_commit)
                title += ["{}..INDEX".format(base_commit or target_commit)]
            else:
                if base_commit and target_commit:
                    prelude += "  {}..{}\n".format(base_commit, target_commit)
                    title += ["{}..{}".format(base_commit, target_commit)]
                elif base_commit and "..." in base_commit:
                    prelude += "  {}\n".format(base_commit)
                    title += [base_commit]
                else:
                    prelude += "  {}..WORKING DIR\n".format(base_commit or target_commit)
                    title += ["{}..WORKING DIR".format(base_commit or target_commit)]
        else:
            if in_cached_mode:
                prelude += "  STAGED CHANGES (Will commit)\n"
                title += ["(staged)"]
            else:
                prelude += "  UNSTAGED CHANGES\n"

        if show_word_diff:
            prelude += "  WORD REGEX: {}\n".format(word_diff_regex)
        if ignore_whitespace:
            prelude += "  IGNORING WHITESPACE\n"

        try:
            diff = self.git(
                "diff",
                "--ignore-all-space" if ignore_whitespace else None,
                "--word-diff-regex={}".format(word_diff_regex) if word_diff_regex else None,
                "--unified={}".format(context_lines) if context_lines is not None else None,
                "--stat" if show_diffstat else None,
                "--patch",
                "--no-color",
                "--cached" if in_cached_mode else None,
                base_commit,
                target_commit,
                "--", file_path)
        except GitSavvyError as err:
            # When the output of the above Git command fails to correctly parse,
            # the expected notification will be displayed to the user.  However,
            # once the userpresses OK, a new refresh event will be triggered on
            # the view.
            #
            # This causes an infinite loop of increasingly frustrating error
            # messages, ultimately resulting in psychosis and serious medical
            # bills.  This is a better, though somewhat cludgy, alternative.
            #
            if err.args and type(err.args[0]) == UnicodeDecodeError:
                self.view.settings().set("git_savvy.disable_diff", True)
                return
            raise err

        old_diff = self.view.settings().get("git_savvy.diff_view.raw_diff")
        self.view.settings().set("git_savvy.diff_view.raw_diff", diff)
        prelude += "\n--\n"

        if word_diff_regex:
            diff, added_regions, removed_regions = postprocess_word_diff(diff, len(prelude))
        else:
            diff, added_regions, removed_regions = diff, [], []

        draw = lambda: _draw(
            self.view,
            ' '.join(title),
            prelude,
            diff,
            bool(word_diff_regex),
            added_regions,
            removed_regions,
            navigate=not old_diff
        )
        if runs_on_ui_thread:
            draw()
        else:
            enqueue_on_ui(draw)


def _draw(view, title, prelude, diff_text, is_word_diff, added_regions, removed_regions, navigate):
    # type: (sublime.View, str, str, str, bool, List[sublime.Region], List[sublime.Region], bool) -> None
    view.set_name(title)
    text = prelude + diff_text
    view.run_command(
        "gs_replace_view_text", {"text": text, "restore_cursors": True}
    )
    if navigate:
        view.run_command("gs_diff_navigate")

    if is_word_diff:
        view.add_regions(
            "git-savvy-added-bold", added_regions, scope="diff.inserted.char.git-savvy.diff"
        )
        view.add_regions(
            "git-savvy-removed-bold", removed_regions, scope="diff.deleted.char.git-savvy.diff"
        )
    else:
        intra_line_colorizer.annotate_intra_line_differences(view, diff_text, len(prelude))


def postprocess_word_diff(text, global_offset=0):
    # type: (str, int) -> Tuple[str, List[sublime.Region], List[sublime.Region]]
    added_regions = []  # type: List[sublime.Region]
    removed_regions = []  # type: List[sublime.Region]

    def extractor(match):
        # We generally transform `{+text+}` (and likewise `[-text-]`) into just
        # `text`.
        text = match.group()[2:-2]
        # The `start/end` offsets are based on the original input, so we need
        # to adjust them for the regions we want to draw.
        total_matches_so_far = len(added_regions) + len(removed_regions)
        start, _end = match.span()
        # On each match the original diff is shortened by 4 chars.
        offset = global_offset + start - (total_matches_so_far * 4)

        regions = added_regions if match.group()[1] == '+' else removed_regions
        regions.append(sublime.Region(offset, offset + len(text)))
        return text

    return WORD_DIFF_MARKERS_RE.sub(extractor, text), added_regions, removed_regions


class GsDiffToggleSetting(TextCommand):

    """
    Toggle view settings: `ignore_whitespace`.
    """

    def run(self, edit, setting):
        settings = self.view.settings()

        setting_str = "git_savvy.diff_view.{}".format(setting)
        current_mode = settings.get(setting_str)
        next_mode = not current_mode
        settings.set(setting_str, next_mode)
        self.view.window().status_message("{} is now {}".format(setting, next_mode))

        self.view.run_command("gs_diff_refresh")


class GsDiffCycleWordDiff(TextCommand):

    """
    Cycle through different word diff patterns.
    """

    def run(self, edit):
        settings = self.view.settings()

        setting_str = "git_savvy.diff_view.{}".format('show_word_diff')
        current_mode = settings.get(setting_str)
        next_mode = (current_mode + 1) % len(WORD_DIFF_PATTERNS)
        settings.set(setting_str, next_mode)

        self.view.run_command("gs_diff_refresh")


class GsDiffToggleCachedMode(TextCommand):

    """
    Toggle `in_cached_mode` or flip `base` with `target`.
    """

    # NOTE: MUST NOT be async, otherwise `view.show` will not update the view 100%!
    def run(self, edit):
        settings = self.view.settings()

        base_commit = settings.get("git_savvy.diff_view.base_commit")
        target_commit = settings.get("git_savvy.diff_view.target_commit")
        if base_commit and target_commit:
            settings.set("git_savvy.diff_view.base_commit", target_commit)
            settings.set("git_savvy.diff_view.target_commit", base_commit)
            self.view.run_command("gs_diff_refresh")
            return

        if base_commit and "..." in base_commit:
            a, b = base_commit.split("...")
            settings.set("git_savvy.diff_view.base_commit", "{}...{}".format(b, a))
            self.view.run_command("gs_diff_refresh")
            return

        last_cursors = settings.get('git_savvy.diff_view.last_cursors') or []
        settings.set('git_savvy.diff_view.last_cursors', pickle_sel(self.view.sel()))

        setting_str = "git_savvy.diff_view.{}".format('in_cached_mode')
        current_mode = settings.get(setting_str)
        next_mode = not current_mode
        settings.set(setting_str, next_mode)
        self.view.window().status_message(
            "Showing {} changes".format("staged" if next_mode else "unstaged")
        )

        self.view.run_command("gs_diff_refresh")

        just_hunked = self.view.settings().get("git_savvy.diff_view.just_hunked")
        # Check for `last_cursors` as well bc it is only falsy on the *first*
        # switch. T.i. if the user hunked and then switches to see what will be
        # actually comitted, the view starts at the top. Later, the view will
        # show the last added hunk.
        if just_hunked and last_cursors:
            self.view.settings().set("git_savvy.diff_view.just_hunked", "")
            region = find_hunk_in_view(self.view, just_hunked)
            if region:
                set_and_show_cursor(self.view, region.a)
                return

        if last_cursors:
            # The 'flipping' between the two states should be as fast as possible and
            # without visual clutter.
            with no_animations():
                set_and_show_cursor(self.view, unpickle_sel(last_cursors))


class GsDiffZoom(TextCommand):
    """
    Update the number of context lines the diff shows by given `amount`
    and refresh the view.
    """
    def run(self, edit, amount):
        # type: (sublime.Edit, int) -> None
        settings = self.view.settings()
        current = settings.get('git_savvy.diff_view.context_lines')
        next = max(current + amount, 0)
        settings.set('git_savvy.diff_view.context_lines', next)

        # Getting a meaningful cursor after 'zooming' is the tricky part
        # here. We first extract all hunks under the cursors *verbatim*.
        diff = SplittedDiff.from_view(self.view)
        cur_hunks = [
            header.text + hunk.text
            for header, hunk in filter_(diff.head_and_hunk_for_pt(s.a) for s in self.view.sel())
        ]

        self.view.run_command("gs_diff_refresh")

        # Now, we fuzzy search the new view content for the old hunks.
        cursors = {
            region.a
            for region in (
                filter_(find_hunk_in_view(self.view, hunk) for hunk in cur_hunks)
            )
        }
        if cursors:
            set_and_show_cursor(self.view, cursors)


class GsDiffFocusEventListener(EventListener):

    """
    If the current view is a diff view, refresh the view with latest tree status
    when the view regains focus.
    """

    def on_activated_async(self, view):
        if view.settings().get("git_savvy.diff_view") is True:
            view.run_command("gs_diff_refresh", {"sync": False})


class GsDiffStageOrResetHunkCommand(TextCommand, GitCommand):

    """
    Depending on whether the user is in cached mode and what action
    the user took, either 1) stage, 2) unstage, or 3) reset the
    hunk under the user's cursor(s).
    """

    # NOTE: The whole command (including the view refresh) must be blocking otherwise
    # the view and the repo state get out of sync and e.g. hitting 'h' very fast will
    # result in errors.

    def run(self, edit, reset=False):
        ignore_whitespace = self.view.settings().get("git_savvy.diff_view.ignore_whitespace")
        show_word_diff = self.view.settings().get("git_savvy.diff_view.show_word_diff")
        if ignore_whitespace or show_word_diff:
            sublime.error_message("You have to be in a clean diff to stage.")
            return None

        # Filter out any cursors that are larger than a single point.
        cursor_pts = tuple(cursor.a for cursor in self.view.sel() if cursor.a == cursor.b)
        diff = SplittedDiff.from_view(self.view)

        patches = unique(flatten(filter_(diff.head_and_hunk_for_pt(pt) for pt in cursor_pts)))
        patch = ''.join(part.text for part in patches)

        if patch:
            self.apply_patch(patch, cursor_pts, reset)
        else:
            window = self.view.window()
            if window:
                window.status_message('Not within a hunk')

    def apply_patch(self, patch, pts, reset):
        in_cached_mode = self.view.settings().get("git_savvy.diff_view.in_cached_mode")
        context_lines = self.view.settings().get('git_savvy.diff_view.context_lines')

        # The three argument combinations below result from the following
        # three scenarios:
        #
        # 1) The user is in non-cached mode and wants to stage a hunk, so
        #    do NOT apply the patch in reverse, but do apply it only against
        #    the cached/indexed file (not the working tree).
        # 2) The user is in non-cached mode and wants to undo a line/hunk, so
        #    DO apply the patch in reverse, and do apply it both against the
        #    index and the working tree.
        # 3) The user is in cached mode and wants to undo a line hunk, so DO
        #    apply the patch in reverse, but only apply it against the cached/
        #    indexed file.
        #
        # NOTE: When in cached mode, no action will be taken when the user
        #       presses SUPER-BACKSPACE.

        args = (
            "apply",
            "-R" if (reset or in_cached_mode) else None,
            "--cached" if (in_cached_mode or not reset) else None,
            "--unidiff-zero" if context_lines == 0 else None,
            "-",
        )
        self.git(
            *args,
            stdin=patch
        )

        history = self.view.settings().get("git_savvy.diff_view.history")
        history.append((args, patch, pts, in_cached_mode))
        self.view.settings().set("git_savvy.diff_view.history", history)
        self.view.settings().set("git_savvy.diff_view.just_hunked", patch)

        self.view.run_command("gs_diff_refresh")


MYPY = False
if MYPY:
    from typing import NamedTuple
    JumpTo = NamedTuple('JumpTo', [
        ('commit_hash', Optional[str]),
        ('filename', str),
        ('row', int),
        ('col', int)
    ])
else:
    from collections import namedtuple
    JumpTo = namedtuple('JumpTo', 'commit_hash filename row col')


class GsDiffOpenFileAtHunkCommand(TextCommand, GitCommand):

    """
    For each cursor in the view, identify the hunk in which the cursor lies,
    and open the file at that hunk in a separate view.
    """

    def run(self, edit):
        # type: (sublime.Edit) -> None

        def first_per_file(items):
            # type: (Iterator[JumpTo]) -> Iterator[JumpTo]
            seen = set()  # type: Set[str]
            for item in items:
                if item.filename not in seen:
                    seen.add(item.filename)
                    yield item

        word_diff_mode = bool(self.view.settings().get('git_savvy.diff_view.show_word_diff'))
        algo = (
            self.jump_position_to_file_for_word_diff_mode
            if word_diff_mode
            else self.jump_position_to_file
        )
        diff = SplittedDiff.from_view(self.view)
        jump_positions = list(first_per_file(filter_(
            algo(diff, s.begin())
            for s in self.view.sel()
        )))
        if not jump_positions:
            util.view.flash(self.view, "Not within a hunk")
        else:
            for jp in jump_positions:
                self.load_file_at_line(*jp)

    def load_file_at_line(self, commit_hash, filename, row, col):
        # type: (Optional[str], str, int, int) -> None
        """
        Show file at target commit if `git_savvy.diff_view.target_commit` is non-empty.
        Otherwise, open the file directly.
        """
        target_commit = commit_hash or self.view.settings().get("git_savvy.diff_view.target_commit")
        full_path = os.path.join(self.repo_path, filename)
        window = self.view.window()
        if not window:
            return

        if target_commit:
            window.run_command("gs_show_file_at_commit", {
                "commit_hash": target_commit,
                "filepath": full_path,
                "lineno": row,
            })
        else:
            window.open_file(
                "{file}:{row}:{col}".format(file=full_path, row=row, col=col),
                sublime.ENCODED_POSITION
            )

    def jump_position_to_file(self, diff, pt):
        # type: (SplittedDiff, int) -> Optional[JumpTo]
        head_and_hunk = diff.head_and_hunk_for_pt(pt)
        if not head_and_hunk:
            return None

        view = self.view
        header, hunk = head_and_hunk

        rowcol = real_rowcol_in_hunk(hunk, relative_rowcol_in_hunk(view, hunk, pt))
        if not rowcol:
            return None

        row, col = rowcol

        filename = header.from_filename()
        if not filename:
            return None

        commit_header = diff.commit_for_hunk(hunk)
        commit_hash = commit_header.commit_hash() if commit_header else None
        return JumpTo(commit_hash, filename, row, col)

    def jump_position_to_file_for_word_diff_mode(self, diff, pt):
        # type: (SplittedDiff, int) -> Optional[JumpTo]
        head_and_hunk = diff.head_and_hunk_for_pt(pt)
        if not head_and_hunk:
            return None

        view = self.view
        header, hunk = head_and_hunk
        content_start = hunk.content().a

        # Select all "deletion" regions in the hunk up to the cursor (pt)
        removed_regions_before_pt = [
            # In case the cursor is *in* a region, shorten it up to
            # the cursor.
            sublime.Region(region.begin(), min(region.end(), pt))
            for region in view.get_regions('git-savvy-removed-bold')
            if content_start <= region.begin() < pt
        ]

        # Count all completely removed lines, but exclude lines
        # if the cursor is exactly at the end-of-line char.
        removed_lines_before_pt = sum(
            region == view.line(region.begin()) and region.end() != pt
            for region in removed_regions_before_pt
        )
        line_start = view.line(pt).begin()
        removed_chars_before_pt = sum(
            region.size()
            for region in removed_regions_before_pt
            if line_start <= region.begin() < pt
        )

        # Compute the *relative* row in that hunk
        head_row, _ = view.rowcol(content_start)
        pt_row, col = view.rowcol(pt)
        rel_row = pt_row - head_row
        # If the cursor is in the hunk header, assume instead it is
        # at `(0, 0)` position in the hunk content.
        if rel_row < 0:
            rel_row, col = 0, 0

        # Extract the starting line at "b" encoded in the hunk header t.i. for
        # "@@ -685,8 +686,14 @@ ..." extract the "686".
        from_start = hunk.header().from_line_start()
        if from_start is None:
            return None
        row = from_start + rel_row

        filename = header.from_filename()
        if not filename:
            return None

        row = row - removed_lines_before_pt
        col = col + 1 - removed_chars_before_pt
        commit_header = diff.commit_for_hunk(hunk)
        commit_hash = commit_header.commit_hash() if commit_header else None
        return JumpTo(commit_hash, filename, row, col)


def relative_rowcol_in_hunk(view, hunk, pt):
    # type: (sublime.View, Hunk, Point) -> RowCol
    """Return rowcol of given pt relative to hunk start"""
    head_row, _ = view.rowcol(hunk.a)
    pt_row, col = view.rowcol(pt)
    # If `col=0` the user is on the meta char (e.g. '+- ') which is not
    # present in the source. We pin `col` to 1 because the target API
    # `open_file` expects 1-based row, col offsets.
    return pt_row - head_row, max(col, 1)


def real_rowcol_in_hunk(hunk, relative_rowcol):
    # type: (Hunk, RowCol) -> Optional[RowCol]
    """Translate relative to absolute row, col pair"""
    hunk_lines = counted_lines(hunk)
    if not hunk_lines:
        return None

    row_in_hunk, col = relative_rowcol

    # If the user is on the header line ('@@ ..') pretend to be on the
    # first visible line with some content instead.
    if row_in_hunk == 0:
        row_in_hunk = next(
            (
                index
                for index, (line, _) in enumerate(hunk_lines, 1)
                if not line.is_from_line() and line.content.strip()
            ),
            1
        )
        col = 1

    line, b = hunk_lines[row_in_hunk - 1]

    # Happy path since the user is on a present line
    if not line.is_from_line():
        return b, col

    # The user is on a deleted line ('-') we cannot jump to. If possible,
    # select the next guaranteed to be available line
    for next_line, next_b in hunk_lines[row_in_hunk:]:
        if next_line.is_to_line():
            return next_b, min(col, len(next_line.content) + 1)
        elif next_line.is_context():
            # If we only have a contextual line, choose this or the
            # previous line, pretty arbitrary, depending on the
            # indentation.
            next_lines_indentation = line_indentation(next_line.content)
            if next_lines_indentation == line_indentation(line.content):
                return next_b, next_lines_indentation + 1
            else:
                return max(1, b - 1), 1
    else:
        return b, 1


def counted_lines(hunk):
    # type: (Hunk) -> Optional[List[HunkLineWithB]]
    """Split a hunk into (first char, line content, row) tuples

    Note that rows point to available rows on the b-side.
    """
    b = hunk.header().from_line_start()
    if b is None:
        return None
    return list(_recount_lines(hunk.content().lines(), b))


def _recount_lines(lines, b):
    # type: (List[HunkLine], int) -> Iterator[HunkLineWithB]

    # Be aware that we only consider the b-line numbers, and that we
    # always yield a b value, even for deleted lines.
    for line in lines:
        yield HunkLineWithB(line, b)
        if not line.is_from_line():
            b += 1


class GsDiffNavigateCommand(GsNavigate):

    """
    Travel between hunks. It is also used by show_commit_view.
    """

    offset = 0

    def get_available_regions(self):
        return self.view.find_by_selector("meta.diff.range.unified, meta.commit-info.header")


class GsDiffUndo(TextCommand, GitCommand):

    """
    Undo the last action taken in the diff view, if possible.
    """

    # NOTE: MUST NOT be async, otherwise `view.show` will not update the view 100%!
    def run(self, edit):
        history = self.view.settings().get("git_savvy.diff_view.history")
        if not history:
            window = self.view.window()
            if window:
                window.status_message("Undo stack is empty")
            return

        args, stdin, cursors, in_cached_mode = history.pop()
        # Toggle the `--reverse` flag.
        args[1] = "-R" if not args[1] else None

        self.git(*args, stdin=stdin)
        self.view.settings().set("git_savvy.diff_view.history", history)
        self.view.settings().set("git_savvy.diff_view.just_hunked", stdin)

        self.view.run_command("gs_diff_refresh")

        # The cursor is only applicable if we're still in the same cache/stage mode
        if self.view.settings().get("git_savvy.diff_view.in_cached_mode") == in_cached_mode:
            set_and_show_cursor(self.view, cursors)


def find_hunk_in_view(view, patch):
    # type: (sublime.View, str) -> Optional[sublime.Region]
    """Given a patch, search for its first hunk in the view

    Returns the region of the first line of the hunk (the one starting
    with '@@ ...'), if any.
    """
    diff = SplittedDiff.from_string(patch)
    try:
        hunk = diff.hunks[0]
    except IndexError:
        return None

    return (
        view.find(hunk.header().text, 0, sublime.LITERAL)
        or fuzzy_search_hunk_content_in_view(view, hunk.content().text.splitlines())
    )


def fuzzy_search_hunk_content_in_view(view, lines):
    # type: (sublime.View, List[str]) -> Optional[sublime.Region]
    """Fuzzy search the hunk content in the view

    Note that hunk content does not include the starting line, the one
    starting with '@@ ...', anymore.

    The fuzzy strategy here is to search for the hunk or parts of it
    by reducing the contextual lines symmetrically.

    Returns the region of the starting line of the found hunk, if any.
    """
    for hunk_content in shrink_list_sym(lines):
        region = view.find('\n'.join(hunk_content), 0, sublime.LITERAL)
        if region:
            diff = SplittedDiff.from_view(view)
            head_and_hunk = diff.head_and_hunk_for_pt(region.a)
            if head_and_hunk:
                _, hunk = head_and_hunk
                hunk_header = hunk.header()
                return sublime.Region(hunk_header.a, hunk_header.b)
            break
    return None


def shrink_list_sym(list):
    # type: (List[T]) -> Iterator[List[T]]
    while list:
        yield list
        list = list[1:-1]


def pickle_sel(sel):
    return [(s.a, s.b) for s in sel]


def unpickle_sel(pickled_sel):
    return [sublime.Region(a, b) for a, b in pickled_sel]


def unique(items):
    # type: (Iterable[T]) -> List[T]
    """Remove duplicate entries but remain sorted/ordered."""
    rv = []  # type: List[T]
    for item in items:
        if item not in rv:
            rv.append(item)
    return rv


def set_and_show_cursor(view, cursors):
    sel = view.sel()
    sel.clear()
    try:
        it = iter(cursors)
    except TypeError:
        sel.add(cursors)
    else:
        for c in it:
            sel.add(c)

    view.show(sel)


@contextmanager
def no_animations():
    pref = sublime.load_settings("Preferences.sublime-settings")
    current = pref.get("animation_enabled")
    pref.set("animation_enabled", False)
    try:
        yield
    finally:
        pref.set("animation_enabled", current)

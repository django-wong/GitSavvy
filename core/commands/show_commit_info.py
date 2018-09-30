import sublime
from sublime_plugin import WindowCommand

from ..git_command import GitCommand


class GsShowCommitInfoCommand(WindowCommand, GitCommand):
    def run(self, commit_hash, file_path=None):
        self._commit_hash = commit_hash
        self._file_path = file_path
        sublime.set_timeout_async(self.run_async)

    def run_async(self):
        show_full = self.savvy_settings.get("show_full_commit_info")
        show_diffstat = self.savvy_settings.get("show_diffstat")
        text = self.git(
            "show",
            "--no-color",
            "--format=fuller",
            "--stat" if show_diffstat else None,
            "--patch" if show_full else None,
            self._commit_hash,
            "--" if self._file_path else None,
            self._file_path if self._file_path else None
        )
        output_view = self.window.create_output_panel("show_commit_info")
        output_view.set_read_only(False)
        output_view.run_command("gs_replace_view_text", {"text": text, "nuke_cursors": True})
        output_view.set_syntax_file("Packages/GitSavvy/syntax/show_commit.sublime-syntax")
        output_view.set_read_only(True)
        self.window.run_command("show_panel", {"panel": "output.show_commit_info"})

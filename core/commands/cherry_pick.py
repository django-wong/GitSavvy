import sublime
from sublime_plugin import TextCommand

from .log import GsLogByBranchCommand
from ...common import util


class GsCherryPickCommand(GsLogByBranchCommand):

    def log(self, **kwargs):
        kwargs["cherry"] = True
        kwargs["start_end"] = ("", self._branch)
        return super().log(**kwargs)

    def do_action(self, commit_hash):
        self.git("cherry-pick", commit_hash)
        sublime.status_message("Commit %s cherry-picked successfully." %
                               commit_hash)
        util.view.refresh_gitsavvy(self.window.active_view())

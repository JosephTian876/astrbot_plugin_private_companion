"""Task-facing role reference: the self page, not the full AstrBot persona."""

from companion_v2.role_profile.context import ConversationRoleProfileContext


class TaskRoleReference:
    """Render the selected role's stable profile for background model tasks."""

    def __init__(self, runtime):
        self.runtime = runtime
        self._context = ConversationRoleProfileContext(runtime)

    def instruction(self, context, at):
        """Return the self-page profile, or an empty string when it is unavailable."""
        try:
            snapshot = self._context.capture(context, at)
            if snapshot is None:
                return ""
            return self._context.instruction(snapshot, context)
        except Exception:
            # A missing or unreadable role profile falls back to the captured
            # persona; it must never stop an already-admitted background task.
            return ""

"""TC-2 substrate: shared session registry + append-only transcript stream.

Storage layout (under `<TESSERACT_HOME>/`):

    tars_controller/
      controller.json          # singleton controller record (TC-4)
      sessions/<id>.json       # one per controller session
      transcripts/<id>.jsonl   # typed event stream
    sessions/chats/<uuid>.json # Mirror multi-chat record (read/write only)

Public surface mirrors the consumers expected by TC-4 / TC-6 / TC-7.
"""

from .daemon import (
    CancelChildWorker,
    ControllerDaemon,
    DispatchTurn,
    ReloadCallback,
)
from .port_probe import (
    controller_port_alive,
    reset_port_alive_cache,
)
from .dispatcher import (
    DispatchMode,
    DispatchOrigin,
    DispatchResult,
    DispatcherError,
    dispatch_to_controller,
    ensure_daemon_running,
    tail_until_assistant_text,
)
from .trust import (
    is_trusted,
    mark_trusted,
    prompt_trust,
    revoke as revoke_trust,
)
from .events import (
    ArtifactEvent,
    AssistantTextEvent,
    BaseTranscriptEvent,
    ChildTranscriptRefEvent,
    GenericTranscriptEvent,
    JournalEntryEvent,
    PermissionRequestEvent,
    PtyChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    TranscriptEvent,
    UserTextEvent,
    WorkerStatusEvent,
    parse_event,
)
from .paths import (
    chats_dir,
    controller_dir,
    controller_record_path,
    heartbeat_path,
    mint_session_id,
    port_file_path,
    run_dir,
    sessions_dir,
    token_file_path,
    transcript_path,
    transcripts_dir,
)
from .ipc_client import ControllerClient, ControllerClientError
from .recovery import TarsControllerRecoveryHandler, register_default_handler
from .shutdown import teardown_all_controller_sessions
from .reload_bridge import ReloadTarget, notify_controller_reload
from .renderer import HEADER, TuiRenderer
from .sessions import ChatRecord, ControllerSessionRecord, SessionRegistry
from .transcript import TranscriptReader, TranscriptWriter

__all__ = [
    "ArtifactEvent",
    "AssistantTextEvent",
    "BaseTranscriptEvent",
    "CancelChildWorker",
    "ChatRecord",
    "ChildTranscriptRefEvent",
    "ControllerClient",
    "ControllerClientError",
    "ControllerDaemon",
    "ControllerSessionRecord",
    "DispatchMode",
    "DispatchOrigin",
    "DispatchResult",
    "DispatchTurn",
    "DispatcherError",
    "dispatch_to_controller",
    "ensure_daemon_running",
    "is_trusted",
    "mark_trusted",
    "prompt_trust",
    "revoke_trust",
    "tail_until_assistant_text",
    "GenericTranscriptEvent",
    "JournalEntryEvent",
    "PermissionRequestEvent",
    "PtyChunkEvent",
    "ReloadCallback",
    "ReloadTarget",
    "SessionRegistry",
    "TarsControllerRecoveryHandler",
    "teardown_all_controller_sessions",
    "ToolResultEvent",
    "ToolUseEvent",
    "TranscriptEvent",
    "TranscriptReader",
    "TranscriptWriter",
    "TuiRenderer",
    "UserTextEvent",
    "WorkerStatusEvent",
    "HEADER",
    "chats_dir",
    "controller_dir",
    "controller_port_alive",
    "controller_record_path",
    "heartbeat_path",
    "mint_session_id",
    "notify_controller_reload",
    "parse_event",
    "port_file_path",
    "register_default_handler",
    "reset_port_alive_cache",
    "run_dir",
    "sessions_dir",
    "token_file_path",
    "transcript_path",
    "transcripts_dir",
]

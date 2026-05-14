"""Canonical event type constants used by both server (main.py WS handler)
and client (chat.html JS). Each constant is the ``type`` field value in the
JSON messages sent over the WebSocket."""

# ─── Lifecycle ────────────────────────────────────────────────
REPLAY_START    = "replay_start"     # replay of buffered events beginning (count, live)
HEARTBEAT       = "heartbeat"        # keepalive ping from server
STATUS          = "status"           # server status message
PONG            = "pong"             # response to client ping

# ─── User messages ────────────────────────────────────────────
USER_MSG        = "user_msg"         # user prompt text

# ─── Assistant streaming ──────────────────────────────────────
ASSISTANT_TEXT  = "assistant_text"   # streaming text chunk from assistant
THINKING_TEXT   = "thinking_text"    # streaming thinking/reasoning chunk
TOOL_USE        = "tool_use"         # tool call started (name, input)
TOOL_RESULT     = "tool_result"      # tool result data
ASK_USER_QUESTION = "ask_user_question"  # question prompt from Claude

# ─── Turn completion ──────────────────────────────────────────
DONE            = "done"             # prompt turn completed (summary, has_denials, usage)
INTERRUPTED     = "interrupted"      # prompt was interrupted by user
ERROR           = "error"            # error from server or process
STDERR          = "stderr"           # stderr output from subprocess

# ─── System ───────────────────────────────────────────────────
SYSTEM_INIT     = "system_init"      # system.init event with session_id

# ─── ACP Permission bridging ──────────────────────────────────
PERMISSION_REQUEST = "permission_request"  # server → client: ACP asks for tool approval
CONFIRM_PERMISSION = "confirm_permission"  # client → server: user's choice (requestId, optionId)

# ─── Mode & Model ─────────────────────────────────────────────
MODE_UPDATE     = "mode_update"      # available modes changed
MODEL_UPDATE    = "model_update"     # available models changed
SET_MODE        = "set_mode"         # client → server: change session mode
SET_MODEL       = "set_model"        # client → server: change model

# ─── Client → Server actions ──────────────────────────────────
PROMPT          = "prompt"           # send a new prompt
INTERRUPT       = "interrupt"        # cancel current generation (process stays alive)
STOP            = "stop"             # close session + kill process
PING            = "ping"             # client keepalive


# ─── Convenience sets ─────────────────────────────────────────
CLIENT_ACTIONS = {PROMPT, INTERRUPT, STOP, PING, SET_MODE, SET_MODEL, CONFIRM_PERMISSION}
STREAM_EVENTS = {ASSISTANT_TEXT, THINKING_TEXT, TOOL_USE, TOOL_RESULT,
                 ASK_USER_QUESTION, DONE, ERROR, INTERRUPTED, STDERR,
                 HEARTBEAT, STATUS, USER_MSG, SYSTEM_INIT,
                 PERMISSION_REQUEST, MODE_UPDATE, MODEL_UPDATE}

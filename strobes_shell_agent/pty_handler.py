"""Interactive PTY handler for the shell bridge agent.

Opens a local pseudo-terminal (bash/zsh) and streams I/O
back through the WebSocket to the Strobes platform.
"""

import asyncio
import logging
import os
import sys

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios

logger = logging.getLogger(__name__)

# Active PTY sessions: session_id -> PtySession
_sessions = {}


class PtySession:
    """Manages a single PTY subprocess."""

    def __init__(self, session_id: str, ws, shell: str = "/bin/bash"):
        self.session_id = session_id
        self.ws = ws
        self.shell = shell
        self.pid = None
        self.fd = None
        self._running = False
        self._read_task = None

    async def start(self, cols: int = 80, rows: int = 24):
        """Open a new PTY and start the shell process."""
        # Find a suitable shell
        shell = self.shell
        for candidate in [os.environ.get("SHELL"), "/bin/bash", "/bin/zsh", "/bin/sh"]:
            if candidate and os.path.exists(candidate):
                shell = candidate
                break

        # Fork a PTY
        self.pid, self.fd = pty.openpty()

        # Set initial terminal size
        self._set_size(cols, rows)

        # Fork the actual shell process
        child_pid = os.fork()
        if child_pid == 0:
            # Child process
            os.close(self.pid)  # Close master side in child
            os.setsid()

            # Set up slave as controlling terminal
            slave_fd = self.fd
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

            # Redirect stdio
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)

            # Set TERM
            os.environ["TERM"] = "xterm-256color"

            # Exec shell
            os.execlp(shell, shell, "--login")
        else:
            # Parent process
            os.close(self.fd)  # Close slave side in parent
            self.fd = self.pid  # Master fd
            self.pid = child_pid

            # Set master to non-blocking
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            self._running = True
            self._read_task = asyncio.create_task(self._read_loop())
            logger.info(f"[PTY] Session {self.session_id} started, pid={self.pid}, shell={shell}")

    async def write(self, data: str):
        """Write input data to the PTY."""
        if self.fd is not None and self._running:
            try:
                os.write(self.fd, data.encode("utf-8"))
            except OSError as e:
                logger.warning(f"[PTY] Write error: {e}")
                await self.stop()

    def resize(self, cols: int, rows: int):
        """Resize the PTY terminal."""
        if self.fd is not None:
            self._set_size(cols, rows)

    def _set_size(self, cols: int, rows: int):
        """Set terminal size via ioctl."""
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except Exception as e:
            logger.debug(f"[PTY] Set size error: {e}")

    async def _read_loop(self):
        """Background loop that reads PTY output and sends via WebSocket."""
        loop = asyncio.get_event_loop()
        try:
            while self._running:
                # Wait for data to be available
                try:
                    readable, _, _ = await loop.run_in_executor(
                        None,
                        lambda: select.select([self.fd], [], [], 0.1)
                    )
                except (ValueError, OSError):
                    break

                if readable:
                    try:
                        data = os.read(self.fd, 4096)
                        if not data:
                            break
                        # Send output back through WebSocket
                        await self.ws.send(
                            __import__("json").dumps({
                                "type": "pty_output",
                                "session_id": self.session_id,
                                "data": data.decode("utf-8", errors="replace"),
                            })
                        )
                    except OSError:
                        break
                    except Exception as e:
                        logger.warning(f"[PTY] Read/send error: {e}")
                        break
        except asyncio.CancelledError:
            pass
        finally:
            logger.info(f"[PTY] Read loop ended for session {self.session_id}")
            # Notify platform the PTY closed
            try:
                await self.ws.send(
                    __import__("json").dumps({
                        "type": "pty_closed",
                        "session_id": self.session_id,
                    })
                )
            except Exception:
                pass

    async def stop(self):
        """Stop the PTY session and kill the shell process."""
        self._running = False

        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                # Wait briefly, then force kill
                await asyncio.sleep(0.5)
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(self.pid, os.WNOHANG)
            except (ProcessLookupError, ChildProcessError):
                pass

        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass

        logger.info(f"[PTY] Session {self.session_id} stopped")


async def handle_pty_open(ws, session_id: str, cols: int = 80, rows: int = 24) -> dict:
    """Open a new PTY session."""
    if IS_WINDOWS:
        return {
            "success": False,
            "error": "Interactive PTY sessions are not supported on Windows.",
        }

    if session_id in _sessions:
        await _sessions[session_id].stop()

    session = PtySession(session_id, ws)
    _sessions[session_id] = session

    try:
        await session.start(cols, rows)
        return {"success": True, "session_id": session_id}
    except Exception as e:
        _sessions.pop(session_id, None)
        return {"success": False, "error": str(e)}


async def handle_pty_input(session_id: str, data: str) -> None:
    """Write input to an existing PTY session."""
    session = _sessions.get(session_id)
    if session:
        await session.write(data)


def handle_pty_resize(session_id: str, cols: int, rows: int) -> None:
    """Resize an existing PTY session."""
    session = _sessions.get(session_id)
    if session:
        session.resize(cols, rows)


async def handle_pty_close(session_id: str) -> dict:
    """Close a PTY session."""
    session = _sessions.pop(session_id, None)
    if session:
        await session.stop()
        return {"success": True}
    return {"success": False, "error": "Session not found"}


async def close_all():
    """Close all PTY sessions (cleanup on disconnect)."""
    for sid in list(_sessions.keys()):
        session = _sessions.pop(sid, None)
        if session:
            await session.stop()

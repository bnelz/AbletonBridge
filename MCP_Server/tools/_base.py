"""Shared tool infrastructure: decorators, helpers, error formatting."""
import asyncio
import functools
import json
import logging

logger = logging.getLogger("AbletonBridge")


def _tool_handler(error_prefix: str):
    """Decorator that wraps tool functions with standard error handling.

    Runs the synchronous tool function in a thread pool via asyncio.to_thread()
    so it doesn't block the FastMCP async event loop during TCP/UDP I/O.

    All returns are wrapped in the standard JSON envelope:
      Success -> tool_success(message) or tool_success(message, data)
      ValueError -> tool_error("Invalid input: ...")
      ConnectionError -> tool_error("M4L bridge not available: ...")
      Exception -> tool_error("Error {prefix}: ...")

    Tools that already return a JSON string (starting with '{') are passed
    through unchanged for backwards compatibility.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                result = await asyncio.to_thread(func, *args, **kwargs)
                # If the tool already returned JSON (e.g. via tool_success or
                # json.dumps), pass it through unchanged.
                if isinstance(result, str) and result.startswith("{"):
                    return result
                # Wrap plain string results in the standard envelope.
                return tool_success(str(result) if result is not None else "ok")
            except ValueError as e:
                return tool_error(f"Invalid input: {e}")
            except ConnectionError as e:
                return tool_error(f"M4L bridge not available: {e}")
            except Exception as e:
                logger.error("Error %s: %s", error_prefix, e)
                return tool_error(f"Error {error_prefix}: {e}")
        return wrapper
    return decorator


def _m4l_result(result: dict) -> dict:
    """Extract result data from M4L response, or raise on error."""
    if result.get("status") == "success":
        return result.get("result", {})
    msg = result.get("message", "Unknown error")
    raise Exception(f"M4L bridge error: {msg}")


def tool_success(message: str, data: dict = None) -> str:
    """Create a standardized success response."""
    result = {"status": "ok", "message": message}
    if data:
        result["data"] = data
    return json.dumps(result)


def tool_error(message: str) -> str:
    """Create a standardized error response."""
    return json.dumps({"status": "error", "message": message})

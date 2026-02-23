import asyncio
import json
import pytest
from unittest.mock import MagicMock
from MCP_Server.tools._base import _tool_handler, _long_running_handler, tool_success, tool_error, _m4l_result


class TestToolHandler:
    @pytest.mark.asyncio
    async def test_basic_success(self):
        @_tool_handler("test operation")
        def my_tool():
            return "success"

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["message"] == "success"

    @pytest.mark.asyncio
    async def test_value_error_caught(self):
        @_tool_handler("test operation")
        def my_tool():
            raise ValueError("bad input")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Invalid input" in parsed["message"]
        assert "bad input" in parsed["message"]

    @pytest.mark.asyncio
    async def test_connection_error_caught(self):
        @_tool_handler("test operation")
        def my_tool():
            raise ConnectionError("no connection")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "M4L bridge not available" in parsed["message"]

    @pytest.mark.asyncio
    async def test_generic_exception_caught(self):
        @_tool_handler("doing stuff")
        def my_tool():
            raise RuntimeError("something broke")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Error doing stuff" in parsed["message"]

    @pytest.mark.asyncio
    async def test_with_args(self):
        @_tool_handler("test")
        def my_tool(a, b):
            return f"{a}+{b}"

        result = await my_tool(1, 2)
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["message"] == "1+2"

    @pytest.mark.asyncio
    async def test_json_passthrough(self):
        """Tools returning JSON strings (e.g. json.dumps) are passed through."""
        @_tool_handler("test")
        def my_tool():
            return json.dumps({"custom": "data"})

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["custom"] == "data"

    @pytest.mark.asyncio
    async def test_none_return(self):
        """Tools returning None get wrapped as tool_success('ok')."""
        @_tool_handler("test")
        def my_tool():
            pass  # returns None

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "ok"


class TestLongRunningHandler:
    """Tests for the progress-reporting decorator."""

    @pytest.mark.asyncio
    async def test_basic_success(self):
        @_long_running_handler("test")
        def my_tool(ctx, report_progress=None):
            return "done"

        ctx = MagicMock()
        result = await my_tool(ctx)
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["message"] == "done"

    @pytest.mark.asyncio
    async def test_json_passthrough(self):
        @_long_running_handler("test")
        def my_tool(ctx, report_progress=None):
            return json.dumps({"result": 42})

        ctx = MagicMock()
        result = await my_tool(ctx)
        parsed = json.loads(result)
        assert parsed["result"] == 42

    @pytest.mark.asyncio
    async def test_error_handling(self):
        @_long_running_handler("loading")
        def my_tool(ctx, report_progress=None):
            raise ValueError("bad")

        ctx = MagicMock()
        result = await my_tool(ctx)
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Invalid input" in parsed["message"]

    @pytest.mark.asyncio
    async def test_progress_callback_provided(self):
        """The report_progress callback should be callable."""
        progress_calls = []

        @_long_running_handler("test")
        def my_tool(ctx, report_progress=None):
            # Report progress should be provided by the decorator
            assert report_progress is not None
            progress_calls.append(True)
            return "done"

        ctx = MagicMock()

        async def _noop_progress(*args):
            pass

        ctx.report_progress = _noop_progress
        await my_tool(ctx)
        assert len(progress_calls) == 1


class TestToolSuccess:
    def test_basic(self):
        result = json.loads(tool_success("Done"))
        assert result["status"] == "ok"
        assert result["message"] == "Done"

    def test_with_data(self):
        result = json.loads(tool_success("Done", {"count": 5}))
        assert result["data"]["count"] == 5


class TestToolError:
    def test_basic(self):
        result = json.loads(tool_error("Failed"))
        assert result["status"] == "error"
        assert result["message"] == "Failed"


class TestM4lResult:
    def test_success(self):
        result = _m4l_result({"status": "success", "result": {"value": 42}})
        assert result["value"] == 42

    def test_error_raises(self):
        with pytest.raises(Exception, match="M4L bridge error"):
            _m4l_result({"status": "error", "message": "device not found"})

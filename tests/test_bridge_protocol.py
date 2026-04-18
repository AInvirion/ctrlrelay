"""Tests for bridge protocol messages."""

import json

import pytest


class TestBridgeOp:
    def test_all_ops_defined(self) -> None:
        """All required operations should be defined."""
        from ctrlrelay.bridge.protocol import BridgeOp

        assert BridgeOp.SEND == "send"
        assert BridgeOp.ASK == "ask"
        assert BridgeOp.ACK == "ack"
        assert BridgeOp.ANSWER == "answer"
        assert BridgeOp.PING == "ping"
        assert BridgeOp.PONG == "pong"
        assert BridgeOp.ERROR == "error"


class TestBridgeMessage:
    def test_send_message(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        msg = BridgeMessage(op=BridgeOp.SEND, request_id="r-001", text="Hello")
        assert msg.op == BridgeOp.SEND
        assert msg.request_id == "r-001"
        assert msg.text == "Hello"

    def test_ask_message_with_options(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        msg = BridgeMessage(
            op=BridgeOp.ASK, request_id="r-002", question="Approve?", options=["yes", "no"]
        )
        assert msg.question == "Approve?"
        assert msg.options == ["yes", "no"]

    def test_ack_message(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        msg = BridgeMessage(op=BridgeOp.ACK, request_id="r-001", status="sent")
        assert msg.status == "sent"

    def test_answer_message(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        msg = BridgeMessage(
            op=BridgeOp.ANSWER, request_id="r-002", answer="yes", answered_at="2026-04-17T12:00:00Z"
        )
        assert msg.answer == "yes"

    def test_error_message(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp
        msg = BridgeMessage(
            op=BridgeOp.ERROR,
            request_id="r-003",
            error="telegram_api_error",
            message="Rate limited",
        )
        assert msg.error == "telegram_api_error"
        assert msg.message == "Rate limited"


class TestSerialize:
    def test_serialize_message(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeMessage, BridgeOp, serialize_message
        msg = BridgeMessage(op=BridgeOp.PING)
        line = serialize_message(msg)
        assert line.endswith("\n")
        data = json.loads(line)
        assert data["op"] == "ping"

    def test_parse_message(self) -> None:
        from ctrlrelay.bridge.protocol import BridgeOp, parse_message
        line = '{"op": "pong"}\n'
        msg = parse_message(line)
        assert msg.op == BridgeOp.PONG

    def test_parse_invalid_json_raises(self) -> None:
        from ctrlrelay.bridge.protocol import ProtocolError, parse_message
        with pytest.raises(ProtocolError, match="Invalid JSON"):
            parse_message("not json\n")

    def test_parse_missing_op_raises(self) -> None:
        from ctrlrelay.bridge.protocol import ProtocolError, parse_message
        with pytest.raises(ProtocolError, match="Missing 'op'"):
            parse_message('{"request_id": "r-001"}\n')

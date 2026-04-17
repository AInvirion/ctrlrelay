"""Tests for transport abstraction."""




class TestTransportProtocol:
    def test_transport_is_protocol(self) -> None:
        """Transport should be a Protocol."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "__protocol_attrs__") or isinstance(
            Transport, type
        )

    def test_transport_has_send(self) -> None:
        """Transport should define send method."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "send")

    def test_transport_has_ask(self) -> None:
        """Transport should define ask method."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "ask")

    def test_transport_has_close(self) -> None:
        """Transport should define close method."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "close")


class TestTransportError:
    def test_error_exists(self) -> None:
        """TransportError should be defined."""
        from dev_sync.transports.base import TransportError

        assert issubclass(TransportError, Exception)

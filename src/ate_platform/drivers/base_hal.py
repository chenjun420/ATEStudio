"""HAL layer — Hardware Abstraction Layer for SCPI/VISA instrument communication.

BaseDriver provides pure SCPI/VISA communication primitives only.
No semantic instrument methods belong here; those go in the MAL layer (base_mal.py).
"""

from __future__ import annotations

import threading
from abc import ABC
from typing import cast

import pyvisa
from pyvisa.resources import MessageBasedResource


class BaseDriver(ABC):
    """Abstract base class for instrument HAL drivers using PyVISA.

    Provides thread-safe communication with instruments via PyVISA.
    Subclasses implement instrument-specific SCPI command routing only —
    no semantic measurement/control methods.
    """

    def __init__(self, resource_manager: pyvisa.ResourceManager | None = None) -> None:
        """Initialize the instrument driver.

        Args:
            resource_manager: Optional PyVISA ResourceManager. If None, creates one with '@py' backend.
        """
        self._resource_manager: pyvisa.ResourceManager | None = resource_manager or pyvisa.ResourceManager("@py")
        self._instrument: MessageBasedResource | None = None
        self._address: str = ""
        self._lock: threading.Lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        """Check if instrument is connected."""
        return self._instrument is not None

    @property
    def address(self) -> str:
        """Get the instrument address."""
        return self._address

    def connect(self, address: str) -> None:
        """Connect to the instrument at the specified address.

        Args:
            address: VISA resource address (e.g., 'TCPIP0::192.168.1.1::INSTR').

        Raises:
            pyvisa.VisaIOError: If connection fails.
            ValueError: If already connected to a different address.
        """
        with self._lock:
            if self._instrument is not None:
                if self._address == address:
                    return  # Already connected to same address
                msg = f"Already connected to {self._address}. Disconnect first."
                raise ValueError(msg)

            # __init__ 保证非空（resource_manager or 新建）；子类（TCP/适配器/模拟）
            # 覆盖了 connect 不走此路径，故此处 None 分支仅为类型收窄，运行时不可达。
            rm = self._resource_manager
            if rm is None:
                msg = "ResourceManager is not initialized"
                raise RuntimeError(msg)
            # open_resource() 静态返回基类 Resource，但平台 HAL 面向 SCPI/VISA
            # 消息类仪器，实际对象为 MessageBasedResource（具备 write/query/read）。
            self._instrument = cast(
                MessageBasedResource,
                rm.open_resource(address),
            )
            self._address = address

    def disconnect(self) -> None:
        """Disconnect from the instrument."""
        with self._lock:
            if self._instrument is not None:
                self._instrument.close()
                self._instrument = None
            self._address = ""

    def write(self, command: str) -> None:
        """Send a command to the instrument.

        Args:
            command: SCPI command to send.

        Raises:
            RuntimeError: If not connected to an instrument.
        """
        with self._lock:
            if self._instrument is None:
                msg = "Not connected to any instrument. Call connect() first."
                raise RuntimeError(msg)
            self._instrument.write(command)

    def query(self, command: str, delay: float | None = None) -> str:
        """Send a query command and read the response.

        Args:
            command: SCPI query command to send.
            delay: Delay in seconds between write and read. None uses instrument default.

        Returns:
            Response string from the instrument.

        Raises:
            RuntimeError: If not connected to an instrument.
        """
        with self._lock:
            if self._instrument is None:
                msg = "Not connected to any instrument. Call connect() first."
                raise RuntimeError(msg)
            kwargs: dict[str, float] = {}
            if delay is not None:
                kwargs["delay"] = delay
            return self._instrument.query(command, **kwargs)

    def read(self) -> str:
        """Read a response from the instrument.

        Returns:
            Response string from the instrument.

        Raises:
            RuntimeError: If not connected to an instrument.
        """
        with self._lock:
            if self._instrument is None:
                msg = "Not connected to any instrument. Call connect() first."
                raise RuntimeError(msg)
            return self._instrument.read()

    def reset(self) -> None:
        """Send *RST to reset the instrument."""
        self.write("*RST")

    def __enter__(self) -> BaseDriver:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit - ensures disconnect."""
        self.disconnect()

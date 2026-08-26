from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

from ate_cloud.models.app_menu import App, AppMenu
from ate_cloud.models.execution import Execution
from ate_cloud.models.fault_event import FaultEvent
from ate_cloud.models.fixture_topology import (
    FixtureDeviceTemplate,
    FixtureTopology,
    FixtureVersion,
)
from ate_cloud.models.node_flow_binding import NodeFlowBinding
from ate_cloud.models.node_template import NodeTemplate
from ate_cloud.models.rbac import Permission, Role
from ate_cloud.models.script import Script
from ate_cloud.models.sequence import Sequence
from ate_cloud.models.user import User

__all__ = [
    "Base",
    "App",
    "AppMenu",
    "Execution",
    "FaultEvent",
    "FixtureDeviceTemplate",
    "FixtureTopology",
    "FixtureVersion",
    "NodeFlowBinding",
    "NodeTemplate",
    "Permission",
    "Role",
    "Script",
    "Sequence",
    "User",
]
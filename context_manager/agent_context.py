"""Multi-agent context support with branching and merging."""

from dataclasses import dataclass, field
from typing import Optional
import time
import uuid
from .interfaces import Message, MessageRole, ContextStore, ContextSnapshot, MemoryLayer, ContextEvent, EventType
from .events import EventBus
from .stores.memory_store import InMemoryContextStore


@dataclass
class AgentContext:
    """Represents a single agent's context branch."""
    agent_id: str
    name: str
    parent_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    branch_point_snapshot: Optional[ContextSnapshot] = None
    tags: list = field(default_factory=list)


class AgentContextManager:
    """Manages multi-agent context with branching and merging support."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self._event_bus = event_bus or EventBus()
        self._branches: dict[str, AgentContext] = {}
        self._active_branch: Optional[str] = None
        self._branch_stores: dict[str, ContextStore] = {}

    def create_branch(
        self,
        name: str,
        parent_agent_id: Optional[str] = None,
        tags: list = None
    ) -> str:
        """Create a new branch from parent context."""
        branch_id = str(uuid.uuid4())

        parent = None
        if parent_agent_id:
            parent = self._branches.get(parent_agent_id)

        branch = AgentContext(
            agent_id=branch_id,
            name=name,
            parent_id=parent.agent_id if parent else None,
            tags=tags or []
        )

        self._branches[branch_id] = branch
        self._branch_stores[branch_id] = InMemoryContextStore()

        self._event_bus.emit(ContextEvent(
            event_type=EventType.BRANCH_CREATED,
            data={"branch": branch},
            source_agent_id=branch_id
        ))

        return branch_id

    def switch_branch(self, branch_id: str) -> None:
        """Switch active context to specified branch."""
        if branch_id not in self._branches:
            raise ValueError(f"Unknown branch: {branch_id}")
        self._active_branch = branch_id

    def get_active_branch(self) -> Optional[str]:
        """Get current active branch ID."""
        return self._active_branch

    def add_message_to_branch(self, branch_id: str, message: Message) -> None:
        """Add message to specific branch."""
        store = self._branch_stores.get(branch_id)
        if store:
            store.add(message)

    def get_branch_context(self, branch_id: str) -> list[Message]:
        """Get all messages for a specific branch."""
        store = self._branch_stores.get(branch_id)
        if store:
            return store.get_all()
        return []

    def merge_branch(
        self,
        source_branch_id: str,
        target_branch_id: Optional[str] = None,
        strategy: str = "selective"
    ) -> None:
        """Merge source branch into target."""
        if source_branch_id not in self._branches:
            raise ValueError(f"Unknown source branch: {source_branch_id}")

        target_id = target_branch_id or self._branches[source_branch_id].parent_id
        if not target_id:
            raise ValueError("No target specified and source has no parent")

        source_store = self._branch_stores.get(source_branch_id)
        if not source_store:
            raise ValueError(f"No store for source branch: {source_branch_id}")

        target_store = self._branch_stores.get(target_id)
        if not target_store:
            raise ValueError(f"No store for target branch: {target_id}")

        if strategy == "selective":
            summary = self._create_merge_summary(source_store.get_all())
            target_store.add(Message(
                role=MessageRole.USER,
                content=f"[MERGED from {source_branch_id}] {summary}"
            ))
        else:
            for msg in source_store.get_all():
                target_store.add(msg)

        self._event_bus.emit(ContextEvent(
            event_type=EventType.BRANCH_MERGED,
            data={
                "source_branch": source_branch_id,
                "target_branch": target_id,
                "strategy": strategy
            }
        ))

        del self._branch_stores[source_branch_id]
        del self._branches[source_branch_id]

    def _create_merge_summary(self, messages: list[Message]) -> str:
        """Create summary for selective merge."""
        lines = [f"[Merged {len(messages)} messages from branch]"]
        for msg in messages[-5:]:
            lines.append(f"{msg.role.value}: {msg.content[:80]}...")
        return "\n".join(lines)

    def list_branches(self) -> list[AgentContext]:
        """List all branches."""
        return list(self._branches.values())

    def get_branch(self, branch_id: str) -> Optional[AgentContext]:
        """Get branch by ID."""
        return self._branches.get(branch_id)
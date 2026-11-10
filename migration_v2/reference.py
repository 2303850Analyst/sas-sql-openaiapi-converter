"""Independent, metadata-only reference registry. Row values never enter a model task."""
from pydantic import Field
from .contracts import Contract, ObjectName, Column, digest


class ReferenceOutput(Contract):
    output: ObjectName
    columns: tuple[Column, ...] = Field(min_length=1)


class ReferenceBundle(Contract):
    schema_version: int = Field(ge=1, le=1)
    run_id: str = Field(min_length=1, max_length=200)
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outputs: tuple[ReferenceOutput, ...] = Field(min_length=1)

    def check_plan(self, plan, snapshot_hash):
        if self.source_snapshot_hash != snapshot_hash or self.parameters_hash != digest(plan.parameters):
            raise ValueError("Reference provenance differs from source/parameters")
        registry = {o.output.key: tuple(c.name for c in o.columns) for o in self.outputs}
        if len(registry) != len(self.outputs):
            raise ValueError("Duplicate reference output")
        expected = {o.name.key: tuple(o.columns) for o in plan.outputs if o.role == "final"}
        if registry != expected:
            raise ValueError("Independent full output registry differs from plan")
        return True

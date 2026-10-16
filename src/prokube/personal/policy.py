"""Personal egress policy inputs. Secrets are referenced by alias, never value."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EgressRule:
    host: str
    protocol: str = "tls"
    credential: str = ""

    def to_dict(self):
        return asdict(self)

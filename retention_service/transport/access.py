"""Authenticated principal bindings and current tenant-scoped grants."""

from dataclasses import dataclass
from typing import NamedTuple


class Denied(Exception):
    """The only outward representation is the shared wire denial."""


@dataclass(frozen=True)
class Scope:
    request_id: str | None = None
    attempt_id: str | None = None


class Credential(NamedTuple):
    fingerprint: str
    uri_san: str
    principal: str


@dataclass(frozen=True)
class Grant:
    principal: str
    tenant: str
    verb: str
    scope: Scope = Scope()

    def permits(self, scope, *, partial=False):
        return all(
            rule is None or value == rule or (partial and value is None)
            for rule, value in ((self.scope.request_id, scope.request_id),
                                (self.scope.attempt_id, scope.attempt_id))
        )


@dataclass(frozen=True)
class ComparisonGrant:
    writer: str
    inspector: str
    tenant: str
    destination: Scope
    reference: Scope
    candidate: Scope


@dataclass(frozen=True)
class ACL:
    revision: str
    credentials: tuple[Credential, ...]
    grants: tuple[Grant, ...]
    comparisons: tuple[ComparisonGrant, ...] = ()

    def __post_init__(self):
        if len({credential.fingerprint for credential in self.credentials}) != len(self.credentials):
            raise ValueError("certificate fingerprint has multiple principal bindings")

    def identify(self, fingerprint, tls):
        for credential in self.credentials:
            if fingerprint == credential.fingerprint and tls.has_uri(credential.uri_san):
                return credential.principal
        return None

    def rules(self, principal, tenant, verb=None):
        return (grant for grant in self.grants if grant.principal == principal and grant.tenant == tenant
                and (verb is None or grant.verb == verb))

    def permits(self, principal, tenant, verb, scope, *, partial=False):
        return any(rule.permits(scope, partial=partial) for rule in self.rules(principal, tenant, verb))


class Access:
    """Passed to the store for final scope checks inside its transaction."""

    def __init__(self, get_acl, identify, principal, tenant):
        self.get_acl = get_acl
        self.identify = identify
        self.principal = principal
        self.tenant = tenant

    def current(self):
        acl = self.get_acl()
        if self.identify(acl) != self.principal:
            raise Denied
        return acl

    def require(self, verb, scope):
        if not self.current().permits(self.principal, self.tenant, verb, scope):
            raise Denied

    def comparison(self, verb, destination, reference, candidate, *, writer):
        """Evaluate each ordered source independently under the current ACL."""
        acl = self.current()
        if verb not in {"admit", "finish"} or not acl.permits(self.principal, self.tenant, verb, destination):
            raise Denied
        for delegation in acl.comparisons:
            if delegation.tenant != self.tenant or delegation.writer != writer:
                continue
            if not acl.permits(delegation.writer, self.tenant, "finish", destination):
                continue
            if not Grant(delegation.writer, self.tenant, "finish", delegation.destination).permits(destination):
                continue
            if all(
                Grant(delegation.writer, self.tenant, "inspect", rule).permits(source)
                and acl.permits(delegation.inspector, self.tenant, "inspect", source)
                for rule, source in ((delegation.reference, reference), (delegation.candidate, candidate))
            ):
                return delegation
        raise Denied

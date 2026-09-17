"""Which backend actually decided the current routing classification.

A ContextVar so the resolver can stamp it into routing_decision without the
backend wrappers and the resolver importing each other. Reset by the routing
node at the start of every turn; a turn whose classification came from the
cache or from an anchored rule never reaches a model and keeps the reset
value.
"""
from contextvars import ContextVar

decided_by: ContextVar[str] = ContextVar("routing_decided_by", default="none")

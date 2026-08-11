"""Provider-aware session identity helpers shared by session and execution records."""

from urllib.parse import quote, unquote

from manager.tasks import TaskError


def manager_session_key(provider, provider_session_id):
    """Return the reversible, provider-aware Manager identity/storage key."""
    if not isinstance(provider, str) or not provider or not isinstance(provider_session_id, str) or not provider_session_id:
        raise TaskError("provider and provider_session_id must be non-empty strings")
    return f"{quote(provider, safe='')}:{quote(provider_session_id, safe='')}"


def parse_manager_session_key(value):
    """Reverse a Manager key; malformed values are not accepted as identities."""
    if not isinstance(value, str) or value.count(":") != 1:
        return None
    provider, provider_session_id = (unquote(part) for part in value.split(":", 1))
    return (provider, provider_session_id) if provider and provider_session_id else None


def session_provider_identity(record):
    """Get provider identity, including legacy records that predate the new field."""
    provider = record.get("provider")
    provider_session_id = record.get("provider_session_id") or record.get("session_id")
    if not isinstance(provider, str) or not provider or not isinstance(provider_session_id, str) or not provider_session_id:
        raise TaskError("session record is missing provider-aware identity")
    return (provider, provider_session_id)


def session_manager_key(record):
    provider, provider_session_id = session_provider_identity(record)
    return manager_session_key(provider, provider_session_id)

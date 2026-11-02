"""Client IP handling with an explicit, single-hop reverse-proxy trust boundary."""

from ipaddress import ip_address

from .store import Rejected


def canonical_ip(value):
    if not isinstance(value, str) or not value or "%" in value:
        raise ValueError("Expected an IPv4 or IPv6 address without a scope identifier")
    parsed = ip_address(value)
    # An IPv4 peer can appear as IPv4-mapped IPv6 on a dual-stack socket.
    mapped = getattr(parsed, "ipv4_mapped", None)
    return str(mapped if mapped is not None else parsed)


def request_ip(request, trusted_proxy_ips):
    try:
        peer = canonical_ip(request.remote)
    except ValueError:
        raise Rejected(400, "Could not determine the client IP address") from None
    if peer not in trusted_proxy_ips:
        # In particular, never accept X-Forwarded-For from a visitor.
        return peer
    values = request.headers.getall("X-Real-IP", [])
    if len(values) != 1:
        raise Rejected(400, "The trusted proxy must supply one X-Real-IP address")
    try:
        return canonical_ip(values[0])
    except ValueError:
        raise Rejected(400, "The trusted proxy supplied an invalid X-Real-IP address") from None

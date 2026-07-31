"""Stage 2: free local-database evidence (authority, prices, charity, artsy).
Same canonical-client pattern as every other consumer; silent-neutral."""
import importlib.util as _il
import os


def _load(name):
    try:
        spec = _il.spec_from_file_location(
            name, os.path.expanduser(f"~/estate-art-scanner/wallhunter/{name}.py"))
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


authority = _load("authority_client")
prices = _load("prices_client")
charity = _load("charity_client")
artsy = _load("artsy_client")


def gather(artist: str, deep: bool = False) -> str:
    """Evidence lines for an artist. `deep` adds the network-touching artsy
    client (only for already-promising lots, mirroring deep.py's flag path)."""
    if not artist or len(artist.split()) < 2:
        return ""
    lines = []
    for client in (authority, prices, charity):
        if client is None:
            continue
        try:
            ln = client.evidence_line(artist)
            if ln:
                lines.append(ln)
        except Exception:
            pass
    if deep and artsy is not None:
        try:
            ln = artsy.evidence_line(artist)
            if ln:
                lines.append(ln)
        except Exception:
            pass
    return "\n".join(lines)


def standing(artist: str) -> str:
    """'strong' | 'listed' | '' — used to force stage-3 review regardless of
    stage-1 promise (institutional names never get dropped by the small model)."""
    if authority is None or not artist or len(artist.split()) < 2:
        return ""
    try:
        info = authority.lookup(artist) if hasattr(authority, "lookup") else None
        if info and isinstance(info, dict) and info.get("standing"):
            return info["standing"]
    except Exception:
        pass
    # fall back to parsing the evidence line
    try:
        ln = authority.evidence_line(artist) or ""
        if "[strong]" in ln:
            return "strong"
        if "[listed]" in ln:
            return "listed"
    except Exception:
        pass
    return ""

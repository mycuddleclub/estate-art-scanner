"""Wall Hunter — per-work visual triage of estate-sale photo galleries.

Detects every artwork visible in a sale's photos (including background and
uncatalogued works), crops and deduplicates them, screens each unique work,
and produces a ranked review report. Reuses the estate-art-scanner fetch
layer (src/estatesales_client.py).
"""

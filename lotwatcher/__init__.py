"""lotwatcher — every-lot LiveAuctioneers + HiBid ingestion on local models.

Daniel's directive (2026-07-31): constantly watch LA for new non-blocked
auctions (~30-50/day), go through every single lot, email flags — same for
HiBid. Local inference = $0 marginal cost; recall over precision.
"""

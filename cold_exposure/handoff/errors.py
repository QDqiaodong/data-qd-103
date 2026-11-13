"""Error type for the hand-off fulfillment marketplace entry."""


class HandoffError(ValueError):
    """A hand-off contract violation, naming the source and a locatable record."""

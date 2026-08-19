"""The capture funnel — one path from a conversation to a memory record.

A channel is an entry point, not a second system. The turn path already worked
that way; capture did not. Three mechanisms wrote the same act three ways: the
Mirror's session store, the channel store's own idle sweep, and an idle
observer fire. They disagreed about when a conversation was over, what the
record looked like, and whether the answer survived a restart.

One funnel now: `sources` reads every entry point into the same
:class:`~tesseract.capture.sources.Conversation`, and `reflect` writes the one
record, tagged with the source it came in through. Adding a channel costs an
adapter and an id.
"""

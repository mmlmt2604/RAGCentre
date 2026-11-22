"""Finding OCR shims and proving that the engine behind each one works.

Finding is passive and cheap (``GET /info`` on known addresses, see ``addresses``). Proving is
active: the parser must read a page that has no text layer (``probe``). ``monitor`` combines the
two into the status the UI shows.
"""

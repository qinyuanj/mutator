# Workers mount overlay views, which needs mutator's namespace. It has to be
# entered before any test starts a thread.
from mutator import sandbox

sandbox.enter()

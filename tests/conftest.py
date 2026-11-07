# Linux workers mount overlay views, which needs mutator's namespace. It has
# to be entered before any test starts a thread. Windows copies need nothing.
from mutator import isolation

isolation.current().enter()

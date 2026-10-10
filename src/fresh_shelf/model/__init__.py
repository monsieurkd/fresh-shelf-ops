"""The decision layer.

Everything above this package answers "what happened". This answers "what should
we do on Tuesday", and the difference imposes two rules that shape every module
here:

1. **Nothing may look at the future.** The demand model sees only data up to the
   day it is predicting, enforced by building features from a strictly lagged
   frame and by backtesting on rolling origins rather than a single holdout.
   A leakage bug here would not raise an error — it would produce a beautiful
   forecast and a plan that fails in the store, which is worse.

2. **The sealed truth is for tests, never for inputs.** `data/truth/*.parquet` is
   the simulator's own record of what happened to the shelf. The models in this
   package read the warehouse, and the test suite compares the two. Reading the
   truth would make every downstream claim circular.
"""

from __future__ import annotations

__all__: list[str] = []

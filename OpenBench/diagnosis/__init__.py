# OpenBench.utils must finish loading first: it imports the views, which import this package back
import OpenBench.utils  # noqa: F401

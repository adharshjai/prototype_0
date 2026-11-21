"""pcldb prototype_0: the lab's data registry and query engine.

Nothing heavy is imported here. The PostgreSQL driver (psycopg) and the Globus SDK are imported
only by the code that needs them, so the SQLite engine runs on a bare Python.
"""

__version__ = "0.1.0"

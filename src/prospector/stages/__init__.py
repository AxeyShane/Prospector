"""Pipeline stages. Each one reads rows the previous stage finished and writes
its own column back, so the database is the queue and the run is resumable."""

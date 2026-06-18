# Procfile – required by Toolforge Build Service
# Each line becomes an executable command inside the container.
#
# "web" is used by webservices (the health-check endpoint).
# "run-bot" is the main pipeline job command.
#
# Note: process type names must NOT collide with real binaries.

web: python main.py --web
run-bot: python main.py

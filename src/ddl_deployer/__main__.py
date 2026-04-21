"""
Enable running the package as a module: python -m ddl_deployer

Delegates to the CLI entry point.
"""

from ddl_deployer.cli import main

if __name__ == "__main__":
    main()

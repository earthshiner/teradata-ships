"""
Enable running the package as a module: python -m database_package_deployer

Delegates to the CLI entry point.
"""

from database_package_deployer.cli import main

if __name__ == "__main__":
    main()

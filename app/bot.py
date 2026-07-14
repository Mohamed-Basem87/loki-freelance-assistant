import asyncio

from app.handlers.telegram import start
from app.logger import initialize_workbook


def main():
    initialize_workbook()
    asyncio.run(start())

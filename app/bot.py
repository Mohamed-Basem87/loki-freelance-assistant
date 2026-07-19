import asyncio

from app.handlers.telegram import start
from app.logger import initialize_workbook
from app.state import state

def main():
    initialize_workbook()
    state.load()
    asyncio.run(start())

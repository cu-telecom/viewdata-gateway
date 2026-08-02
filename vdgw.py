import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdgw")

BRIDGE_CHUNK_SIZE = 4096
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_CHOICE_TIMEOUT = 120  # seconds a client has to pick a menu option / send a byte


# Borrrowed from John Newcombe - https://bitbucket.org/johnnewcombe/telstar-server-1.0/src
def edittf_decode(data, row_begin=1, row_end=22, column_begin=0, column_end=39, trim_ends=True):
    """
    Decodes the selected portion of edit.tf data into Prestel format, returns a string.
    :param row_begin:
    :param row_end:
    :param column_start:
    :param column_end:
    :return:
    """

    # col start must be < col end and row begin < row end etc.
    if row_end <= row_begin or column_end <= column_begin:
        raise IndexError

    # decode the url to get raw data
    raw_data = parse_edittf_url(data)
    cols_to_take = column_end - column_begin + 1

    # result goes here
    rows_out = []

    # Teletext is 25 lines, Prestel/Telstar is 24, in addition line 0 is reserved for the Telstar header
    # and line 23 (24th line) is reserved for system messages, therefore
    # ignore first and last two lines of the raw data
    for row_index in range(row_begin, row_end + 1):

        # get the row, offset and restricted to the requested column window
        base = row_index * 40 + column_begin
        row = raw_data[base:base + cols_to_take]

        # if this blob is to be contatenated then the call will probably
        # not want this trimmed
        if trim_ends:
            row = row.rstrip()

        chars = []
        for ch in row:
            asc = ord(ch)

            # for values 00 - 1F, add 40 and precede with an escape
            if 0x00 <= asc <= 0x1f:
                asc += 0x40
                chars.append('\x1b')
                chars.append(chr(asc))
            else:
                chars.append(chr(asc))
        rows_out.append(''.join(chars))

        # as rstrip is used for each row, the row could be shorter than the requested width
        if len(row) < cols_to_take:
            rows_out.append('\r\n')

    return ''.join(rows_out)


# Decodes the url returning raw teletext data
def parse_edittf_url(encoded_url):

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    # If the URL contains a hash, remove everything up to and including it.
    hash_pos = encoded_url.find('#')
    encoded_data = encoded_url[hash_pos + 3:]

    colon_pos = encoded_data.find(':')
    if colon_pos > 0:
        encoded_data = encoded_data[:colon_pos]

    length = len(encoded_data)

    if length != 1120 and length != 1167:
        raise ValueError(f"The encoded frame should be exactly 1120 or 1167 characters in length, it was {length}")

    # creates a populated list all 0's
    decoded_data = [0 for _ in range(1000)]

    for index in range(0, len(encoded_data)):

        # this returns the index of the letter in alphabet that corresponds to the char in the url
        findex = alphabet.find(encoded_data[index])

        if findex == -1:
            raise ValueError(f"The encoded character at position {index} should be one from the alphabet")

        for b in range(0, 6):

            # $val holds the index of the char

            bit = findex & (1 << (5 - b))

            if bit > 0:
                cbit = (index * 6) + b
                cpos = cbit % 7
                cloc = int((cbit - cpos) / 7)
                decoded_data[cloc] |= 1 << (6 - cpos)

    # decoded data is a list of integers, so iterate over it and perform a join to get a string
    return ''.join([chr(n) for n in decoded_data])


def insert_menu_status(original: str, ascii_str: str) -> str:

    # Split the original data by the newline character to get individual rows
    rows = original.split('\x0A')

    # Pad with empty rows if needed
    while len(rows) < 22:
        rows.append('')

    # Ensure that the ASCII string doesn't exceed the expected row width (40 characters)
    ascii_str = ascii_str[:40]

    # Replace or insert content into row 23
    if len(rows) >= 22:
        rows[21] = ascii_str
    else:
        rows.append(ascii_str)

    # Reconstruct the data
    updated_binary = '\x0A'.join(rows)
    return updated_binary


def load_config(path="config.yaml"):
    try:
        with open(path, "r") as file:
            cfg = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as e:
        logger.error("Couldn't load %s: %s", path, e)
        sys.exit(1)

    for required in ("listening_port", "menu_url", "backend_servers"):
        if required not in cfg:
            logger.error("%s is missing required key '%s'", path, required)
            sys.exit(1)

    return cfg


config = load_config()

# The menu is decoded once at startup rather than per-connection, since it never
# changes at runtime and decoding it is pure CPU work with no benefit to redoing
# it for every client.
menu = edittf_decode(config["menu_url"])

max_connections = config.get("max_connections", DEFAULT_MAX_CONNECTIONS)
choice_timeout = config.get("choice_timeout", DEFAULT_CHOICE_TIMEOUT)
connection_semaphore = asyncio.Semaphore(max_connections)


def generate_date_string():
    # YYYYMMDDT00HHMMZ
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT00%H%MZ")


async def relay(reader, writer):
    try:
        while True:
            data = await reader.read(BRIDGE_CHUNK_SIZE)
            if not data:
                return
            writer.write(data)
            await writer.drain()
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def close_writer(writer):
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass


async def handle_client(reader, writer):
    client_address = "{}:{}".format(*writer.get_extra_info('peername'))

    if connection_semaphore.locked():
        logger.warning("%s rejected: max connections (%s) reached", client_address, max_connections)
        await close_writer(writer)
        return

    async with connection_semaphore:
        logger.info("%s connected", client_address)
        try:
            await serve_client(reader, writer, client_address)
        finally:
            await close_writer(writer)
            logger.info("%s disconnected", client_address)


async def serve_client(reader, writer, client_address):
    writer.write(b"\x0c" + generate_date_string().encode() + b"\x0c")
    writer.write(menu.encode())
    await writer.drain()

    while True:  # Keep the outer loop for displaying the menu again
        attempts = 0
        max_attempts = 3
        garbage = 0
        max_garbage = 10
        backend = None
        choice_data = b""

        while attempts < max_attempts and garbage < max_garbage:
            try:
                choice_data = await asyncio.wait_for(reader.read(1), timeout=choice_timeout)
            except asyncio.TimeoutError:
                logger.info("%s timed out waiting for a menu choice", client_address)
                return

            if not choice_data:
                logger.info("%s disconnected while at the menu", client_address)
                return

            if choice_data.isdigit():
                choice = int(choice_data.decode())
                backend = config['backend_servers'].get(choice)
                if backend:
                    logger.info("%s selected #%s, connecting to %s:%s", client_address, choice, backend['host'], backend['port'])
                    break
                else:
                    logger.info("%s entered an invalid choice: %s", client_address, choice)
                    writer.write(b"\x0c")
                    status_message = insert_menu_status(menu, "\x1B\x48\x1B\x41Invalid Choice. Try again")
                    writer.write(status_message.encode())
                    await writer.drain()
                    attempts += 1
            else:
                garbage += 1
                logger.info("%s sent non-numeric character: %s garbage: %s", client_address, choice_data, garbage)

        if not backend:
            logger.info("%s failed too many attempts. Disconnecting", client_address)
            writer.write(b"\x0c")
            status_message = insert_menu_status(menu, "\x1B\x48\x1B\x41Too many failed attempts. Goodbye")
            writer.write(status_message.encode())
            await writer.drain()
            return

        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(backend['host'], backend['port']), timeout=3)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("%s couldn't connect to %s:%s - %s", client_address, backend['host'], backend['port'], e)
            writer.write(b"\x0c")
            status_message = insert_menu_status(menu, "\x1B\x48\x1B\x41Connection failed. Try another")
            writer.write(status_message.encode())
            await writer.drain()
            continue

        try:
            client_to_backend = asyncio.create_task(relay(reader, backend_writer))
            backend_to_client = asyncio.create_task(relay(backend_reader, writer))

            _, pending = await asyncio.wait(
                {client_to_backend, backend_to_client},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await close_writer(backend_writer)

        logger.info("%s session with %s:%s ended", client_address, backend['host'], backend['port'])
        return


async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    server = await asyncio.start_server(handle_client, '0.0.0.0', config['listening_port'])
    logger.info("Listening on port %s (max_connections=%s)", config['listening_port'], max_connections)

    async with server:
        await stop_event.wait()
        logger.info("Shutting down")


if __name__ == "__main__":
    asyncio.run(main())

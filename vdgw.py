import asyncio
import logging
import math
import signal
import sys
from datetime import datetime, timezone

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdgw")

BRIDGE_CHUNK_SIZE = 4096
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_CHOICE_TIMEOUT = 120  # seconds a client has to pick a menu option / send a byte
DEFAULT_BANNER_ROWS = 10
ROW_WIDTH = 40
FRAME_ROWS = 22  # usable rows per frame; row 22 (index 21) is always reserved for status messages

# Minimal Telnet (RFC 854) option negotiation, for clients that speak telnet
# before falling back to raw Viewdata bytes. We don't support any options, so
# every WILL/DO is met with a flat refusal; WONT/DONT need no reply.
TELNET_IAC = 0xFF
TELNET_WILL = 0xFB
TELNET_WONT = 0xFC
TELNET_DO = 0xFD
TELNET_DONT = 0xFE
TELNET_REFUSAL = {TELNET_WILL: TELNET_DONT, TELNET_DO: TELNET_WONT}


# Borrrowed from John Newcombe - https://bitbucket.org/johnnewcombe/telstar-server-1.0/src
def edittf_decode(data, row_begin=1, row_end=22, column_begin=0, column_end=39, trim_ends=True):
    """
    Decodes the selected portion of edit.tf data into Prestel format.
    Returns a list with one entry per requested row; each entry already carries
    its own trailing '\\r\\n' when the row is narrower than the requested width
    (a full-width row is left as-is, since the terminal wraps on its own).
    """

    # col start must be < col end and row begin < row end etc.
    if row_end <= row_begin or column_end <= column_begin:
        raise IndexError

    # decode the url to get raw data
    raw_data = parse_edittf_url(data)
    cols_to_take = column_end - column_begin + 1

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

        content = ''.join(chars)

        # as rstrip is used for each row, the row could be shorter than the requested width
        if len(row) < cols_to_take:
            content += '\r\n'

        rows_out.append(content)

    return rows_out


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


def pad_row(text):
    """Truncates to the row width and appends a line-end unless the row is full width."""
    text = text[:ROW_WIDTH]
    if len(text) < ROW_WIDTH:
        return text + '\r\n'
    return text


def with_status_row(rows, text):
    """Returns a copy of rows with row 22 (index 21) replaced by a status message."""
    updated = list(rows)
    updated[FRAME_ROWS - 1] = pad_row(text)
    return updated


def render_frame(rows):
    return ''.join(rows).encode()


def build_pages(banner, backends, banner_row_count):
    """
    Builds one complete 22-row frame per page: the banner rows, followed by an
    auto-generated list of backends for that page (digit-labelled), a "# More"
    footer when there's more than one page, blank padding, and a placeholder
    row for status messages.

    Returns (pages, page_backends) - pages[i] is a ready-to-send list of 22
    row strings, page_backends[i] is the list of backend dicts selectable by
    digit on that page.
    """
    list_row_count = (FRAME_ROWS - 1) - banner_row_count  # rows available below the banner, above the status row
    if list_row_count < 1:
        logger.error("banner_rows (%s) leaves no room for the backend list", banner_row_count)
        sys.exit(1)

    total = len(backends)
    max_per_page = min(10, list_row_count)

    if total <= max_per_page:
        entries_per_page = total if total > 0 else 1
        show_footer = False
    else:
        entries_per_page = min(10, list_row_count - 1)
        show_footer = True

    num_pages = max(1, math.ceil(total / entries_per_page)) if total > 0 else 1

    pages = []
    page_backends = []
    for page_index in range(num_pages):
        group = backends[page_index * entries_per_page:(page_index + 1) * entries_per_page]
        page_backends.append(group)

        rows = list(banner)
        for digit, backend in enumerate(group):
            rows.append(pad_row(f"{digit} {backend['name']}"))

        blank_rows_needed = list_row_count - len(group) - (1 if show_footer else 0)
        rows.extend(pad_row('') for _ in range(blank_rows_needed))

        if show_footer:
            rows.append(pad_row("# More options"))

        rows.append(pad_row(''))  # placeholder for the status row (row 22)
        pages.append(rows)

    return pages, page_backends


def load_config(path="config.yaml"):
    try:
        with open(path, "r") as file:
            cfg = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as e:
        logger.error("Couldn't load %s: %s", path, e)
        sys.exit(1)

    for required in ("listening_port", "banner_url", "backend_servers"):
        if required not in cfg:
            logger.error("%s is missing required key '%s'", path, required)
            sys.exit(1)

    for entry in cfg["backend_servers"]:
        for field in ("name", "host", "port"):
            if field not in entry:
                logger.error("%s: backend_servers entry missing '%s': %r", path, field, entry)
                sys.exit(1)

    return cfg


config = load_config()

# The banner is decoded and the pages are built once at startup rather than
# per-connection, since none of it changes at runtime and it's pure CPU work
# with no benefit to redoing it for every client.
banner_row_count = config.get("banner_rows", DEFAULT_BANNER_ROWS)
banner = edittf_decode(config["banner_url"], row_begin=1, row_end=banner_row_count)
pages, page_backends = build_pages(banner, config["backend_servers"], banner_row_count)

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
    current_page = 0

    writer.write(b"\x0c" + generate_date_string().encode() + b"\x0c")
    writer.write(render_frame(pages[current_page]))
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

            if choice_data[0] == TELNET_IAC:
                try:
                    command = await asyncio.wait_for(reader.readexactly(2), timeout=choice_timeout)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    logger.info("%s disconnected during telnet negotiation", client_address)
                    return

                cmd, option = command[0], command[1]
                reply = TELNET_REFUSAL.get(cmd)
                if reply is not None:
                    writer.write(bytes([TELNET_IAC, reply, option]))
                    await writer.drain()
                continue

            if choice_data == b'#':
                current_page = (current_page + 1) % len(pages)
                logger.info("%s moved to menu page %s/%s", client_address, current_page + 1, len(pages))
                writer.write(b"\x0c")
                writer.write(render_frame(pages[current_page]))
                await writer.drain()
                continue

            if choice_data.isdigit():
                choice = int(choice_data.decode())
                backends_here = page_backends[current_page]
                backend = backends_here[choice] if choice < len(backends_here) else None
                if backend:
                    logger.info("%s selected #%s, connecting to %s:%s", client_address, choice, backend['host'], backend['port'])
                    break
                else:
                    logger.info("%s entered an invalid choice: %s", client_address, choice)
                    writer.write(b"\x0c")
                    writer.write(render_frame(with_status_row(pages[current_page], "\x1B\x48\x1B\x41Invalid Choice. Try again")))
                    await writer.drain()
                    attempts += 1
            else:
                garbage += 1
                logger.info("%s sent non-numeric character: %s garbage: %s", client_address, choice_data, garbage)

        if not backend:
            logger.info("%s failed too many attempts. Disconnecting", client_address)
            writer.write(b"\x0c")
            writer.write(render_frame(with_status_row(pages[current_page], "\x1B\x48\x1B\x41Too many failed attempts. Goodbye")))
            await writer.drain()
            return

        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(backend['host'], backend['port']), timeout=3)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("%s couldn't connect to %s:%s - %s", client_address, backend['host'], backend['port'], e)
            writer.write(b"\x0c")
            writer.write(render_frame(with_status_row(pages[current_page], "\x1B\x48\x1B\x41Connection failed. Try another")))
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
    logger.info("Listening on port %s (max_connections=%s, pages=%s)", config['listening_port'], max_connections, len(pages))

    async with server:
        await stop_event.wait()
        logger.info("Shutting down")


if __name__ == "__main__":
    asyncio.run(main())

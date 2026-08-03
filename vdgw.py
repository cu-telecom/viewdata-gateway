import asyncio
import logging
import math
import signal
import sys

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdgw")

BRIDGE_CHUNK_SIZE = 4096
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_CHOICE_TIMEOUT = 120  # seconds a client has to pick a menu option / send a byte
DEFAULT_BANNER_ROWS = 10
ROW_WIDTH = 40
FRAME_ROWS = 22  # usable content rows per frame (banner + auto-generated list)

# Minimal Telnet (RFC 854) option negotiation, for clients that speak telnet
# before falling back to raw Viewdata bytes. We don't support any options, so
# every WILL/DO is met with a flat refusal; WONT/DONT need no reply.
TELNET_IAC = 0xFF
TELNET_WILL = 0xFB
TELNET_WONT = 0xFC
TELNET_DO = 0xFD
TELNET_DONT = 0xFE
TELNET_REFUSAL = {TELNET_WILL: TELNET_DONT, TELNET_DO: TELNET_WONT}

# Viewdata/Prestel alphanumeric colour codes (ESC + code + 0x40)
COLOUR_BLACK = "\x1B\x40"
COLOUR_RED = "\x1B\x41"
COLOUR_GREEN = "\x1B\x42"
COLOUR_YELLOW = "\x1B\x43"
COLOUR_BLUE = "\x1B\x44"
COLOUR_CYAN = "\x1B\x46"
COLOUR_WHITE = "\x1B\x47"
NEW_BACKGROUND = "\x1B\x5D"  # sets the background to whatever alpha colour was set just before it
BLACK_BACKGROUND = "\x1B\x5C"  # sets the background directly to black, independent of the current alpha colour
STEADY = "\x1B\x49"  # cancels Flash - defensive, in case it was left set by something earlier

MESSAGE_DISPLAY_SECONDS = 2  # how long transient full-page messages (connecting, errors) stay up

# Real Viewdata terminals don't use plain ASCII: the physical "#" (hash) key
# transmits 0x5F, and displaying ASCII 0x23 renders as something else (a "$"
# or a "£" depending on the client) rather than a hash. 0x5F round-trips
# correctly on both input and display, so it's used for both here - anywhere
# a hash needs to be shown or matched, use HASH_CHAR/HASH_BYTE, never a
# literal '#'.
HASH_BYTE = b'\x5f'
HASH_CHAR = '\x5f'
MAX_DIGIT_BUFFER = 4  # generous headroom above any realistic backend count

INPUT_PROMPT = f"Enter selection + {HASH_CHAR} : "
HEADER_TITLE = "VDGW"  # visible text only, used for width/padding calculations - the colour codes themselves provide the spacing between letters


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


def center_in_row(text, invisible_cells):
    """
    Centres `text` within a full ROW_WIDTH-cell row that's preceded by
    `invisible_cells` attribute-code cells (colour codes etc). Those cells
    show nothing but still occupy screen space and always sit on the left,
    so naively centring within the remaining visible cells biases the text
    right of the true row centre - the left padding needs to be shorter than
    the right by `invisible_cells` to compensate.
    """
    available = ROW_WIDTH - invisible_cells
    text = text[:available]
    total_gap = available - len(text)
    left_gap = max(0, (total_gap - invisible_cells) // 2)
    right_gap = total_gap - left_gap
    return (' ' * left_gap) + text + (' ' * right_gap)


def build_input_bar():
    """
    The default bottom-bar content: a blue-background, yellow-text prompt
    with no padding or trailing line-end after it, so the cursor is left
    sitting right after the prompt text. Digits the client then types are
    echoed back as raw bytes in serve_client, appearing one after another
    purely because the cursor advances with each transmitted character -
    no cursor-addressing escape sequence is used or needed. 3 attribute
    codes (blue fg, new background, yellow fg) is the minimum possible for
    a non-black background - there's no dedicated single-code shortcut for
    blue the way there is for black.
    """
    return f"{COLOUR_BLUE}{NEW_BACKGROUND}{COLOUR_YELLOW}{INPUT_PROMPT}"


def build_header(current_page, num_pages):
    """The row-0 header: "VDGW" on the left, spaced by their own colour codes' invisible cells, "N/M" page indicator in yellow-on-black flush right."""
    title = f"{COLOUR_GREEN}V{COLOUR_RED}D{COLOUR_CYAN}G{COLOUR_BLUE}W"
    indicator = f"{current_page + 1}/{num_pages}"
    left_invisible = 4  # one colour code per letter (green, red, cyan, blue)
    right_invisible = 3  # black background, steady, yellow fg for the indicator
    # Deliberately 1 cell short of the full row width, ending with an explicit
    # \r\n instead of relying on the terminal to auto-wrap after exactly 40
    # cells - some terminals defer that wrap until the next character, and an
    # explicit newline arriving right after can double-advance, producing an
    # extra blank line before the frame content.
    padding = max(0, (ROW_WIDTH - 1) - len(HEADER_TITLE) - left_invisible - right_invisible - len(indicator))
    return f"{title}{' ' * padding}{BLACK_BACKGROUND}{STEADY}{COLOUR_YELLOW}{indicator}\r\n"


def build_message_frame(text, colour):
    """A blank frame with `text` in `colour`, centred on the page - used for transient full-page messages."""
    rows = [pad_row('') for _ in range(FRAME_ROWS)]
    middle_row = FRAME_ROWS // 2
    centred = center_in_row(text, invisible_cells=1)  # one leading colour code
    rows[middle_row] = f"{colour}{centred}"  # exactly full width - no line-end needed
    rows.append(pad_row(''))  # status row
    return rows


def render_frame(rows):
    return ''.join(rows).encode()


def build_pages(banner, backends, banner_row_count):
    """
    Builds one complete frame per page: 22 content rows (the banner, followed
    by an auto-generated list of backends ("N) name") numbered globally and
    continuously across all pages - not restarting at each page - a "#) More"
    entry when there's more than one page, and blank padding), plus one
    further bottom-bar row below them (see build_input_bar) showing the input
    prompt, with typed digits echoed live after it - see serve_client. The
    "N/M" page indicator itself lives in the header (see build_header), not
    on this row. Errors (invalid selection, too many attempts) are shown as
    their own transient full-page messages instead of overriding this row -
    see invalid_selection_frame/too_many_attempts_frame in serve_client.

    Because numbering is global, a client can type a number they saw on a
    different page (e.g. "15") and it resolves correctly regardless of which
    page is currently on screen - see the global `backend_servers` lookup in
    serve_client, which indexes the full list directly rather than a
    per-page slice.

    Returns pages - pages[i] is a ready-to-send list of FRAME_ROWS + 1 row strings.
    """
    list_row_count = FRAME_ROWS - banner_row_count  # rows available below the banner, within the 22 content rows
    if list_row_count < 1:
        logger.error("banner_rows (%s) leaves no room for the backend list", banner_row_count)
        sys.exit(1)

    total = len(backends)
    index_width = len(str(total - 1)) if total > 0 else 1  # pad single-digit numbers to align with the widest one

    # Always reserve at least one blank row as a gap before the input bar,
    # plus one more for the "#) More" entry when there's more than one page.
    if total <= list_row_count - 1:
        entries_per_page = total if total > 0 else 1
        show_footer = False
    else:
        entries_per_page = max(1, list_row_count - 2)
        show_footer = True

    num_pages = max(1, math.ceil(total / entries_per_page)) if total > 0 else 1

    pages = []
    for page_index in range(num_pages):
        start = page_index * entries_per_page
        group = backends[start:start + entries_per_page]

        rows = list(banner)
        for offset, backend in enumerate(group):
            global_index = start + offset
            colour = COLOUR_YELLOW if global_index % 2 == 0 else COLOUR_WHITE
            rows.append(pad_row(f"{colour}{global_index:>{index_width}}) {backend['name']}"))

        if show_footer:
            more_colour = COLOUR_YELLOW if (start + len(group)) % 2 == 0 else COLOUR_WHITE
            rows.append(pad_row(f"{more_colour}{HASH_CHAR:>{index_width}}) More"))

        blank_rows_needed = list_row_count - len(group) - (1 if show_footer else 0)
        rows.extend(pad_row('') for _ in range(max(1, blank_rows_needed)))

        rows.append(build_input_bar())  # bottom bar: input prompt with live digit echo
        pages.append(rows)

    return pages


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
all_backends = config["backend_servers"]
pages = build_pages(banner, all_backends, banner_row_count)
connecting_frame = render_frame(build_message_frame("CONNECTING", COLOUR_GREEN))
connection_failed_frame = render_frame(build_message_frame("CONNECTION FAILED", COLOUR_RED))
invalid_selection_frame = render_frame(build_message_frame("Invalid selection", COLOUR_YELLOW))
too_many_attempts_frame = render_frame(build_message_frame("Too many attempts", COLOUR_YELLOW))

max_connections = config.get("max_connections", DEFAULT_MAX_CONNECTIONS)
choice_timeout = config.get("choice_timeout", DEFAULT_CHOICE_TIMEOUT)
connection_semaphore = asyncio.Semaphore(max_connections)


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


async def send_page(writer, current_page, frame=None):
    """Sends the header (with an up-to-date page indicator) followed by a page frame, clearing first."""
    header = build_header(current_page, len(pages)).encode()
    if frame is None:
        frame = render_frame(pages[current_page])
    # No clear between header and frame: the header is exactly 40 visible
    # cells wide, so it wraps into row 1 on its own - an extra \x0c here
    # would clear/home the display again and wipe the header out.
    writer.write(b"\x0c" + header + frame)
    await writer.drain()


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

    await send_page(writer, current_page)

    while True:  # Keep the outer loop for displaying the menu again
        attempts = 0
        max_attempts = 3
        garbage = 0
        max_garbage = 10
        backend = None
        choice_data = b""
        digit_buffer = ""

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

            if choice_data == HASH_BYTE:
                if not digit_buffer:
                    # Hash with nothing typed first means "show the next page"
                    current_page = (current_page + 1) % len(pages)
                    logger.info("%s moved to menu page %s/%s", client_address, current_page + 1, len(pages))
                    await send_page(writer, current_page)
                    continue

                choice = int(digit_buffer)
                digit_buffer = ""
                backend = all_backends[choice] if 0 <= choice < len(all_backends) else None
                if backend:
                    logger.info("%s selected #%s, connecting to %s:%s", client_address, choice, backend['host'], backend['port'])
                    break
                else:
                    logger.info("%s entered an invalid choice: %s", client_address, choice)
                    writer.write(b"\x0c")
                    writer.write(invalid_selection_frame)
                    await writer.drain()
                    await asyncio.sleep(MESSAGE_DISPLAY_SECONDS)
                    await send_page(writer, current_page)
                    attempts += 1
                continue

            if choice_data.isdigit():
                digit_buffer += choice_data.decode()
                # Echoed as a raw byte - the cursor simply advances one cell per
                # character, landing right after the input prompt built into
                # the bottom bar (see build_input_bar), with no cursor-jump needed.
                writer.write(choice_data)
                await writer.drain()
                if len(digit_buffer) > MAX_DIGIT_BUFFER:
                    digit_buffer = ""
                    garbage += 1
                    logger.info("%s sent too many digits without a hash, discarding: garbage: %s", client_address, garbage)
                continue

            garbage += 1
            logger.info("%s sent non-numeric character: %s garbage: %s", client_address, choice_data, garbage)

        if not backend:
            logger.info("%s failed too many attempts. Disconnecting", client_address)
            writer.write(b"\x0c")
            writer.write(too_many_attempts_frame)
            await writer.drain()
            await asyncio.sleep(MESSAGE_DISPLAY_SECONDS)
            return

        writer.write(b"\x0c")
        writer.write(connecting_frame)
        await writer.drain()

        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(backend['host'], backend['port']), timeout=3)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("%s couldn't connect to %s:%s - %s", client_address, backend['host'], backend['port'], e)
            writer.write(b"\x0c")
            writer.write(connection_failed_frame)
            await writer.drain()
            await asyncio.sleep(MESSAGE_DISPLAY_SECONDS)
            await send_page(writer, current_page)
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

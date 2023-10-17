import asyncio
import aiofiles
import yaml
from datetime import datetime


# Borrrowed from John Newcombe - https://bitbucket.org/johnnewcombe/telstar-server-1.0/src
async def edittf_decode(data, row_begin = 1, row_end = 22, column_begin = 0, column_end = 39, trim_ends = True):

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
        raw_data = await parse_edittf_url(data)
        cols_to_take = column_end - column_begin + 1

        # result goes here
        blob = ''

        # Teletext is 25 lines, Prestel/Telstar is 24, in addition line 0 is reserved for the Telstar header
        # and line 23 (24th line) is reserved for system messages, therefore
        # ignore first and last two lines of the raw data
        row_num = 0
        for row in range (row_begin, row_end + 1):

            row_num += 1

            # TODO: sort out row length... after the following strip

            # get the row but restrict to the row length specified
            row = raw_data[row*40:row*40 + 40][0:cols_to_take]

            # if this blob is to be contatenated then the call will probably
            # not want this trimmed
            if trim_ends == True:
                row = row.rstrip()

            rlen = len(row)

            for col in range(column_begin, min(cols_to_take + 1, rlen)):
                asc = ord(row[col])

                # for values 00 - 1F, add 40 and precede with an escape
                if 0x00 <= asc <= 0x1f:
                    asc += 0x40
                    blob += '\x1b'
                    blob += chr(asc)
                    #print('Viewdata Code: {0} {1}'.format(hex(0x1b), hex(asc)))
                else:
                    blob += chr(asc)
                    #print('Viewdata Code: {0} [{1}]'.format(hex(asc)[2:].zfill(2), chr(asc)))

            # as rstrip is used for each row, the row could be shorter than 40 chars
            if rlen < 40:
                blob += '\r\n'

        return blob


# Decodes the url returning raw teletext data
async def parse_edittf_url(encoded_url):

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    # If the URL contains a hash, remove everything up to and including it.
    hash_pos = encoded_url.find('#')
    encoded_data = encoded_url[hash_pos+3:]

    colon_pos = encoded_data.find(':')
    if(colon_pos > 0):
        encoded_data = encoded_data[:colon_pos]

    length = len(encoded_data)

    if length != 1120 and length != 1167 :
        print("The encoded frame should be exactly 1120 or 1167 characters in length, it was {0}".format(length))
        # TODO Create custom exception
        raise Exception()

    # creates a populated list all 0's
    decoded_data = [0 for i in range(1000)]

    for index in range (0,len(encoded_data)):

        # this returns the index of the letter in alphabet that corresponds to the char in the url
        findex = alphabet.find(encoded_data[index])

        if findex == -1 :
            print("The encoded character at position findex should be one from the alphabet")
            # TODO Create custom exception
            raise Exception()

        for b in range(0,6):

            # $val holds the index of the char

            bit = findex & ( 1 << ( 5 - b ))

            if bit > 0:
                cbit = (index * 6) + b
                cpos = cbit % 7
                cloc = int((cbit-cpos) / 7)
                decoded_data[cloc] |= 1 << (6 - cpos)

    # decoded data is a list of integers, so iterate over it and perform a join to get a string
    return ''.join([chr(n) for n in decoded_data])

# No longer used but as it was working lets keep it.
# async def insert_header(original: str) -> str:

#     ascii_str = config['header']
#     # Split the original data by the newline character to get individual rows
#     rows = original.split('\x0A')

#     # Ensure that the ASCII string doesn't exceed the expected row width (40 characters)
#     ascii_str = ascii_str[:40]

#     # Replace the content in the first row
#     rows[0] = ascii_str

#     # Reconstruct the data
#     updated_binary = '\x0A'.join(rows)
#     return updated_binary


async def insert_menu_status(original: str, ascii_str: str) -> str:

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


# Load the configuration from the YAML file
with open("config.yaml", "r") as file:
    config = yaml.safe_load(file)


def generate_date_string():
    # YYYYMMDDT
    date_part = datetime.utcnow().strftime("%Y%m%dT")

    # HHMM
    time_part = datetime.utcnow().strftime("%H%M")

    # Construct the final date-time string in the format YYYYMMDDT00HHMMZ
    final_string = date_part + "00" + time_part + "Z"
    return final_string


async def bridge(reader, writer):
    try:
        while True:
            data = await reader.read(100)
            if not data:
                raise ConnectionResetError()
            writer.write(data)
            await writer.drain()
    except:
        raise

async def handle_client(reader, writer):
    client_address = "{}:{}".format(*writer.get_extra_info('peername'))
    print(f"{client_address} connected")

    menu = await edittf_decode(config['menu_url'])
    writer.write(b"\x0c" + generate_date_string().encode() + b"\x0c")
    writer.write(menu.encode())
    await writer.drain()

    while True:  # Keep the outer loop for displaying the menu again
        attempts = 0
        max_attempts = 3
        garbage = 0
        max_garbage = 10

        while attempts < max_attempts and garbage < max_garbage:
            choice_data = await reader.read(1)
            if choice_data.isdigit():
                choice = int(choice_data.decode().strip())
                backend = config['backend_servers'].get(choice)
                if backend:
                    print(f"{client_address} selected #{choice_data.decode('utf-8')}. Attempting to connect to {backend['host']}:{backend['port']}")
                    break
                else:
                    print(f"{client_address} entered an invalid choice: {choice_data.decode('utf-8')}")
                    writer.write(b"\x0c")
                    status_message = await insert_menu_status(menu, "\x1B\x48\x1B\x41Invalid Choice. Try again")
                    writer.write(status_message.encode())
                    await writer.drain()
                    attempts += 1
            else:
                garbage += 1
                print(f"{client_address} sent non-numeric character: {choice_data} garbage: {garbage}")


        if attempts >= max_attempts or garbage >= max_garbage:
            print(f"{client_address} failed too many attempts. Disconnecting")
            writer.write(b"\x0c")
            status_message = await insert_menu_status(menu, "\x1B\x48\x1B\x41Too many failed attempts. Goodbye")
            writer.write(status_message.encode())
            await writer.drain()
            writer.close()
            return

        try:
            backend_reader, backend_writer = await asyncio.wait_for(asyncio.open_connection(backend['host'], backend['port']), timeout=3)
        except:
            print(f"{client_address} couldn't connect to service #{choice_data.decode('utf-8')} {backend['host']}:{backend['port']}")
            writer.write(b"\x0c")
            status_message = await insert_menu_status(menu, "\x1B\x48\x1B\x41Connection failed. Try another")
            writer.write(status_message.encode())
            await writer.drain()
            continue

        # Now handle the bridge. If an exception is raised here, it means the client was disconnected after a successful connection
        try:
            await asyncio.gather(bridge(reader, backend_writer), bridge(backend_reader, writer))
        except:
            print(f"{client_address} was disconnected from #{choice_data.decode('utf-8')} {backend['host']}:{backend['port']}. Disconnecting")
            writer.close()
            return

async def main():

    server = await asyncio.start_server(handle_client, '0.0.0.0', config['listening_port'])
    async with server:
        await server.serve_forever()

asyncio.run(main())


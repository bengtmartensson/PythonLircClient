#! /usr/bin/env python3

# Copyright (C) 2017 Bengt Martensson.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see http://www.gnu.org/licenses/.

"""
This is a new and independent implementation of the Lirc irsend(1) program.
It offers a Python API and a command line interface. The command line
interface is almost, but not quite, compatible with irsend. Instead, it is
organized as a program with subcommands, send_once, etc.

There are some other subtile differences from irsend:

* subcommand must be lower case,
* send_once only takes one command (irsend takes several),
* send_stop without arguments uses the remote and the command from the
  last send_start command (API only; not from the command line),
* no need to give dummy empty arguments for commands like list,
* The --count argument to send_once is argument to the subcommand.
* the code in list remote is suppressed, unless -c is given,
* port number must be given with the --port (-p) argument; hostip:portnumber
  is not recognized,
* verbose option --verbose (-v)
* selectable timeout with --timeout (-t) option
* better error messages

It does not depend on anything but standard Python libraries.

The name comes from the fact that the program requsts services from
a Lirc server (lircd). It has nothing to do with the library
lirc_client in Lirc.

For a GUI alternative, look at IrScrutinizer.
For a Java alternative, look at JavaLircClient
https://github.com/bengtmartensson/JavaLircClient
"""

import argparse
import enum
import socket
import sys
import re
import os

VERSION = "LircClient 0.1.0"
DEFAULT_LIRC_DEVICE = '/var/run/lirc/lircd'
DEFAULT_PORT = 8765

_READCHUNKLENGTH = 4096


class LircServerException(Exception):
    """This exception is thrown when the Lirc server responds with an error."""
    pass


class BadPacketException(Exception):
    """This exception is thrown when a communication error occurs"""
    pass


class ThisCannotHappenException(Exception):
    """
    This exception is thrown when an 'impossible' condition occurs,
    most likely a programming error.
    """
    pass


class ClientInstantiationError(Exception):
    """Thrown if the LircClient cannot be instantiated."""
    pass


class Result(enum.Enum):
    ''' Public reply parser result, available when completed. '''
    OK = 1
    FAIL = 2
    INCOMPLETE = 3


class ReplyParser(object):
    '''
    Handles parsing of reply packet. Public accessors:
       - result: Enum Result, reflects parser state.
       - success: boolean, reflects SUCCESS/ERROR.
       - data: List of lines, the command DATA payload.
       - sighup: boolean, reflects if SIGHUP package has been received
         (these are otherwise ignored)
       - last_line: string, last input line (for error messages).
       - is_completed: True if no more input is required.
    '''

    def __init__(self):
        self.result = Result.INCOMPLETE
        self.success = None
        self.data = []
        self.last_line = ""
        self.sighup = False
        self._state = self._State.BEGIN
        self._lines_expected = None
        self._buffer = bytearray(0)

    @property
    def is_completed(self):
        ''' Returns true if no more reply input is required. '''
        return self.result != Result.INCOMPLETE

    def feed(self, line):
        ''' Enter a line of data into parsing FSM, update state. '''

        fsm = {
            self._State.BEGIN: self._begin,
            self._State.COMMAND: self._command,
            self._State.RESULT: self._result,
            self._State.DATA: self._data,
            self._State.LINE_COUNT: self._line_count,
            self._State.LINES: self._lines,
            self._State.END: self._end,
            self._State.SIGHUP_END: self._sighup_end
        }
        line = line.strip()
        if not line:
            return
        self.last_line = line
        fsm[self._state](line)
        if self._state == self._State.DONE:
            self.result = Result.OK

    ##
    #  @defgroup FSM Internal parser FSM
    #  @{
    #  pylint: disable=missing-docstring,redefined-variable-type

    class _State(enum.Enum):
        ''' Internal FSM state. '''
        BEGIN = 1
        COMMAND = 2
        RESULT = 3
        DATA = 4
        LINE_COUNT = 5
        LINES = 6
        END = 7
        DONE = 8
        NO_DATA = 9
        SIGHUP_END = 10

    def _bad_packet_exception(self, line):
        raise BadPacketException(
            "Cannot parse: %s\nat state: %s\n" % (line, self._state))

    def _begin(self, line):
        if line == "BEGIN":
            self._state = self._State.COMMAND

    def _command(self, line):
        if not line:
            self._bad_packet_exception(line)
        self._state = self._State.RESULT

    def _result(self, line):
        if line in ["SUCCESS", "ERROR"]:
            self.success = line == "SUCCESS"
            self._state = self._State.DATA
        elif line == "SIGHUP":
            self._state = self._State.SIGHUP_END
            self.sighup = True
        else:
            self._bad_packet_exception(line)

    def _data(self, line):
        if line == "END":
            self._state = self._State.DONE
        elif line == "DATA":
            self._state = self._State.LINE_COUNT
        else:
            self._bad_packet_exception(line)

    def _line_count(self, line):
        try:
            self._lines_expected = int(line)
        except ValueError:
            self._bad_packet_exception(line)
        if self._lines_expected == 0:
            self._state = self._State.END
        else:
            self._state = self._State.LINES

    def _lines(self, line):
        self.data.append(line)
        if len(self.data) >= self._lines_expected:
            self._state = self._State.END

    def _end(self, line):
        if line != "END":
            self._bad_packet_exception(line)
        self._state = self._State.DONE

    def _sighup_end(self, line):
        if line == "END":
            self._state = self._State.BEGIN
        else:
            self._bad_packet_exception(line)
    ## @}
    #  pylint: enable=missing-docstring,redefined-variable-type


class AbstractLircClient:
    """
    Abstract base class for the LircClient. To implement the class,
    the abstract "socket" needs to be assigned to something sensible.
    Public properties:
         - timeout: ms, the timeout used in server communication.
         - verbose: boolean, if True print some progress info
    """

    def __init__(self, verbose, timeout):
        self.timeout = timeout
        self.verbose = verbose
        self._parser = ReplyParser()
        self._socket = None
        self._in_buffer = bytearray(0)
        self._last_command = None
        self._last_remote = None

    def close(self):
        """Close the connection."""
        self._socket.close()

    def _read_line(self):
        """
        Return a line read from the socket.
        The input from the socket is buffered.
        """
        newline = b'\n'
        while newline not in self._in_buffer:
            self._in_buffer += self._socket.recv(_READCHUNKLENGTH)
        line, self._in_buffer = self._in_buffer.split(newline, 1)
        return line.decode("US-ASCII")

    def _send_string(self, cmd):
        """Sends a string to the Lirc server."""
        self._socket.send(bytearray(cmd, 'US-ASCII'))

    # This function should preferrably not be made public, although
    # it may be tempting...
    def _send_command(self, packet):
        """
        Sends its argument string to the Lirc server,
        and receives zero or more lines in response.
        Returns a list of those lines.
        """
        if self.verbose:
            print("Sending: `" + packet
                  + "' to Lirc@" + self._socket.__str__())

        self._send_string(packet + '\n')

        result = []
        success = True

        while not self._parser.is_completed:
            string = self._read_line()
            string.strip()
            if not string:
                continue
            if self.verbose:
                print('Received: "{0}"'.format(string or ''))
            self._parser.feed(string)
        if self.verbose:
            print("Command " +
                  ("succeded." if self._parser.success else "failed."))
        if not success:
            raise LircServerException(''.join(result))
        return self._parser.data

    def send_ir_command(self, remote, command, count):
        """
        Requests the Lirc server to transmit the named commmand,
        belonging to the named remote, the stated number of times.
        (The number of repeats in the sense of lircd(8) will be one less.)
        """
        self._last_remote = remote
        self._last_command = command
        return self._send_command(
            "SEND_ONCE " + remote + " " + command + " " + str(count - 1)) \
            is not None

    def send_ir_command_repeat(self, remote, command):
        """
        Requests the Lirc server to start transmitting the named command from
        the named remote,
        until either explicitly stopped by a corresponding stop_ir command,
        or a server-specific limit is reached.
        """
        self._last_remote = remote
        self._last_command = command
        return self._send_command(
            "SEND_START " + remote + " " + command) is not None

    def stop_ir(self, remote=None, command=None):
        """
        Requests the Lirc server to stop transmitting the named command from
        the named remote. If and only if the start_ir_command_repeat
        has been previously used, the remote and command values can
        be left out, in which case the old values are used.
        """
        return self._send_command(
            "SEND_STOP "
            + (remote if remote else self._last_remote)
            + " " + (command if command else self._last_command)) \
            is not None

    def get_remotes(self):
        """
        Returns a list of the names of the remotes known
        to the Lirc server.
        """
        return self._send_command("LIST")

    def get_commands(self, remote, include_codes=False):
        """
        Returns a list of the commands contained in the remote
        given as argument.
        If the optional argument include_codes is True,
        the hexadecimal codes of
        the commands are also given, like irsend does.
        """
        raw = self._send_command("LIST " + remote)
        if include_codes:
            return raw
        result = []
        for cmd in raw:
            result.append(re.sub(r'^[0-9a-fA-F]* +', '', cmd))
        return result

    def set_transmitters(self, transmitters):
        """
        Requests the Lirc server to use the given transmitters for
        future transmissions.
        The argument should be a list of integers,
        corresponding to the desired
        transmitters. The first transmitter has number 1.
        Not all Lirc servers accept
        or implement this command.
        Note that error messages from Lircd are not always reliable.
        If the Lirc server gives an error, a LircServerException is thrown.
        """
        mask = 0
        for transmitter in transmitters:
            mask |= (1 << (int(transmitter) - 1))
        return self.set_transmitters_mask(mask) is not None

    def set_transmitters_mask(self, mask):
        """
        Requests the Lirc server to use the given transmitters by
        the arguments for
        future transmissions.
        The argument is an integer, were bit n is set if the n+1 transmitter
        is to be enabled.
        The first transmitter has number 1.
        Not all Lirc servers accept
        or implement this command.
        Note that error messages from Lircd are not always reliable.
        If the Lirc server gives an error,
        a LircServerException is thrown.
        """
        s = "SET_TRANSMITTERS " + str(mask)
        return self._send_command(s) is not None

    def get_version(self):
        """Returns the version string of the Lirc server."""
        result = self._send_command("VERSION")
        version = result[0]
        return version

    def set_input_log(self, path):
        """Sets the input log path to lircd. If None. inhibit logging."""
        self._send_command("SET_INPUTLOG " + path or "")
        # FIXME: error handling

    def set_driver_option(self, key, value):
        """Sets a driver option to teh given value."""
        self._send_command("DRV_OPTION " + key + " " + value)

    def simulate(self, event_string):
        """
        Sends the argument string uninterpreted to the Lirc server
        for simulation.
        """
        self._send_command("SIMULATE " + event_string)


class UnixDomainSocketLircClient(AbstractLircClient):
    """"
    This class implements the LircClient with a Unix Domain Socket,
    typically /var/run/lirc/lircd.
    """
    def __init__(self, socketAddress=DEFAULT_LIRC_DEVICE, verbose=False):
        AbstractLircClient.__init__(self, verbose, None)
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(socketAddress)


class TcpLircClient(AbstractLircClient):
    """
    This class implements the LirClient using a TCP network socket,
    per default on port 8765.
    """

    def __init__(self, address="localhost",
                 port=DEFAULT_PORT, verbose=False, timeout=None):
        AbstractLircClient.__init__(self, verbose, timeout)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.connect((address, port))
        self._socket.settimeout(timeout)


def _new_lirc_client(command_line_args):
    """
    Factory method that returns a concrete subclass of the LircClient,
    depending on the argument.
    """
    try:
        return UnixDomainSocketLircClient(command_line_args.socket_pathname,
                                          command_line_args.verbose) \
            if command_line_args.address is None else \
            TcpLircClient(command_line_args.address,
                          command_line_args.port,
                          command_line_args.verbose,
                          command_line_args.timeout)
    except Exception as ex:
        raise ClientInstantiationError(ex)


def parse_commandline():
    """ Parse command line args and options, returns a ArgumentParser. """
    parser = argparse.ArgumentParser(prog='LircClient')
    parser.add_argument(
        "-a", "--address",
        help='IP name or address of lircd host. '
        + 'Takes preference over --device.',
        dest='address', default=None)
    socket_path = os.environ['LIRC_SOCKET_PATH'] \
        if 'LIRC_SOCKET_PATH' in os.environ else DEFAULT_LIRC_DEVICE
    parser.add_argument(
        '-d', '--device',
        help='Path name of the lircd socket',
        dest='socket_pathname', default=socket_path)
    parser.add_argument(
        '-p', '--port',
        help='Port of lircd, default ' + str(DEFAULT_PORT),
        dest='port', default=DEFAULT_PORT, type=int)
    parser.add_argument(
        '-t', '--timeout',
        help='Timeout in milliseconds',
        dest='timeout', type=int, default=None)
    parser.add_argument(
        '-V', '--version',
        help='Display version information for this program',
        dest='versionRequested', action='store_true')
    parser.add_argument(
        '-v', '--verbose',
        help='Have some commands executed verbosely',
        dest='verbose', action='store_true')

    subparsers = parser.add_subparsers(dest='subcommand')

    # Command send_once
    parser_send_once = subparsers.add_parser(
        'send_once',
        help='Send one command')
    parser_send_once.add_argument(
        '-#', '-c', '--count',
        help='Number of times to send command in send_once',
        dest='count', type=int, default=1)
    parser_send_once.add_argument('remote', help='Name of remote')
    parser_send_once.add_argument('command', help='Name of command')

    # Command send_start
    parser_send_start = subparsers.add_parser(
        'send_start',
        help='Start sending one command until stopped')
    parser_send_start.add_argument('remote', help='Name of remote')
    parser_send_start.add_argument('command', help='Name of command')

    # Command send_stop
    parser_send_stop = subparsers.add_parser(
        'send_stop',
        help='Stop sending the command from send_start')
    parser_send_stop.add_argument('remote', help='remote command')
    parser_send_stop.add_argument('command', help='remote command')

    # Command list
    parser_list = subparsers.add_parser(
        'list',
        help='Inquire either the list of remotes,'
        + ' or the list of commands in a remote')
    parser_list.add_argument(
        "-c", "--codes",
        help='List the numerical codes in lircd.conf, not only the names',
        dest='codes', action='store_true')
    parser_list.add_argument(
        'remote', nargs='?',
        help='Name of remote; empty for a list of remotes')

    # Command set_input_logging
    parser_set_input_log = subparsers.add_parser(
        'set_input_log',
        help='Set input logging')
    parser_set_input_log.add_argument(
        'log_file', nargs='?',
        help='Path to log file, empty to inhibit logging', default='')

    # Command set_driver_options
    parser_set_driver_options = subparsers.add_parser(
        'set_driver_option',
        help='Set driver option')
    parser_set_driver_options.add_argument('key', help='Name of the option')
    parser_set_driver_options.add_argument('value', help='Option value')

    # Command simulate
    # The user must find out syntax & semantics of the even string himself ;-)
    parser_simulate = subparsers.add_parser(
        'simulate',
        help='Fake the reception of IR signals')
    parser_simulate.add_argument(
        'event_string',
        help='Event string to send to the Lirc server (ONE argument!)')

    # Command set_transmitters
    parser_set_transmitters = subparsers.add_parser(
        'set_transmitters',
        help='Set transmitters')
    parser_set_transmitters.add_argument(
        'transmitters', nargs='+',
        help="transmitter...")

    # Command version
    subparsers.add_parser(
        'version',
        help='Inquire version of the Lirc server. '
        + ' (Use "--version" for the version of this program.)')

    return parser.parse_args()


def main():
    """Interface between the command line and the classes."""

    def do_list(args):
        """ Run the LIST [remote] socket command. """
        if args.remote:
            result = lirc.get_commands(args.remote, args.codes)
        else:
            result = lirc.get_remotes()
        for line in result:
            print(line)

    commands = {
        'send_once':
            lambda: lirc.send_ir_command(args.remote,
                                         args.command,
                                         args.count),
        'send_start':
            lambda: lirc.send_ir_command_repeat(args.remote, args.command),
        'send_stop':
            lambda: lirc.stop_ir(args.remote, args.command),
        'list':
            lambda: do_list(args),
        'set_driver_option':
            lambda: lirc.set_driver_option(args.key, args.value),
        'simulate':
            lambda: lirc.simulate(args.event_string),
        'set_transmitters':
            lambda: lirc.set_transmitters(args.transmitters),
        'set_input_log':
            lambda: lirc.set_input_log(args.log_file),
        'version':
            lambda: print(lirc.get_version()),
    }
    args = parse_commandline()
    if args.versionRequested:
        print(VERSION)
        sys.exit(0)

    lirc = None
    try:
        lirc = _new_lirc_client(args)
    except ClientInstantiationError as ex:
        print("Cannot instantiate the client: {0}".format(ex))
        sys.exit(2)

    try:
        exitstatus = 0
        if args.subcommand in commands:
            commands[args.subcommand]()
        else:
            print('Unknown or missing subcommand, use --help for syntax.')
            exitstatus = 1

    except LircServerException as ex:
        print("LircServerError: {0}".format(ex))
        exitstatus = 3
    except BadPacketException as ex:
        print("Malformed or unexpected package received: {0}".format(ex))
        exitstatus = 4

    if lirc:
        lirc.close()

    sys.exit(exitstatus)


if __name__ == "__main__":
    main()

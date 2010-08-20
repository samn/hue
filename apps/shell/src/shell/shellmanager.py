# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module handles I/O with shells.  Much of the functionality has been pushed down into the
Shell class itself, but all routing occurs through the ShellManager.
"""

import cStringIO
import errno
import fcntl
import logging
import os
import signal
import shell.conf
import shell.constants as constants
import shell.utils as utils
import subprocess
import time
import tornado.ioloop
import desktop.lib.i18n

LOG = logging.getLogger(__name__)

class TimestampedConnection(object):
  """
  A class to wrap Tornado request handlers with timestamps.
  """
  def __init__(self, handler):
    self.handler = handler
    self.time_received = time.time()

class Shell(object):
  """
  A class to encapsulate I/O with a shell subprocess.
  """
  def __init__(self, shell_command, shell_id):
    subprocess_env = {}
    env = desktop.lib.i18n.make_utf8_env()
    for item in constants.PRESERVED_ENVIRONMENT_VARIABLES:
      value = env.get(item)
      if value:
        subprocess_env[item] = value
    LOG.debug("Subprocess environment is %s" % (subprocess_env,))
    p = subprocess.Popen(shell_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           shell=True, stderr=subprocess.STDOUT, env=subprocess_env, close_fds=True)

    # Get the file descriptors
    ifd = p.stdin.fileno()
    ofd = p.stdout.fileno()

    # Now set them to nonblocking mode
    self._set_nonblocking(ifd)
    self._set_nonblocking(ofd)

    # State that isn't touched by any other classes.
    self._ifd = ifd
    self._ofd = ofd
    self._io_loop = tornado.ioloop.IOLoop.instance()
    self._write_callback_enabled = False
    self._read_callback_enabled = False
    self._smanager = ShellManager.global_instance()
    self._subprocess = p
    self._write_buffer = cStringIO.StringIO()
    self._read_buffer = cStringIO.StringIO()
    self._prompt_connections = [] # A list of connections whose commands have yet to be sent to the
                                  # subprocess
    # Since output connections are multiplexed, we store a set of Hue instance IDs. Each such ID
    # uniquely identifies a browser tab. When output is received from a subprocess, we write to
    # an output pipe to as many of these tabs as possible.  Having the indirection from IDs to
    # output connections allows us to avoid writing down the same output pipe twice, which would
    # cause an error.
    self._output_connection_ids = set()
    # Metadata for output reliability. For each chunk of output stores the start and end points
    # in the read buffer. This is a list of tuples, so the ith element tells us the start and
    # end points of the ith chunk of output.
    self._output_chunk_info = []

    # State that's accessed by other classes.
    self.shell_id = shell_id
    self.last_output_sent = False
    self.remove_at_next_iteration = False
    # Timestamp that is updated on shell creation and on every output request. Used so that we know 
    # when to kill the shell.
    self.time_received = time.time()

  def _set_nonblocking(self, fd):
    """
    Set the file descriptor passed in to nonblocking mode.
    """
    fd_attr = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fd_attr | os.O_NONBLOCK)

  def get_previous_output(self):
    """
    Called when a Hue session is restored. Returns a tuple of ( all previous output, next chunk id).
    """
    return ( self._read_buffer.getvalue(), len(self._output_chunk_info) )

  def command_received(self, command, connection):
    """
    Called when a command is received from the client. If we have room, we add the command to the
    write buffer. Otherwise, we tell the client that there is no more room in the write buffer.
    """
    LOG.debug("Command received for shell %s : '%s'" % (self, command,))
    if len(self._write_buffer.getvalue()) >= constants.WRITE_BUFFER_LIMIT:
      LOG.debug("Write buffer too full, dropping command")
      utils.write(connection, { constants.BUFFER_EXCEEDED : True }, True)
    else:
      LOG.debug("Write buffer has room. Adding command to end of write buffer.")
      self._append_to_write_buffer(command)
      self.enable_write_callback()
      self._prompt_connections.append(connection)

  def enable_write_callback(self):
    """
    Register a callback with the global IOLoop for when the child becomes writable.
    """
    if not self._write_callback_enabled:
      self._io_loop.add_handler(self._ifd, self._child_writable, self._io_loop.WRITE)
      self._write_callback_enabled = True
      
  def enable_read_callback(self):
    """
    Register a callback with the global IOLoop for when the child becomes readable.
    """
    if not self._read_callback_enabled:
      self._io_loop.add_handler(self._ofd, self._child_readable, self._io_loop.READ)
      self._read_callback_enabled = True

  def disable_read_callback(self):
    """
    Unregister the _child_readable callback from the global IOLoop.
    """
    if self._read_callback_enabled:
      self._io_loop.remove_handler(self._ofd)
      self._read_callback_enabled = False
  
  def disable_write_callback(self):
    """
    Unregister the _child_writable callback from the global IOLoop.
    """
    if self._write_callback_enabled:
      self._io_loop.remove_handler(self._ifd)
      self._write_callback_enabled = False
  
  def mark_for_cleanup(self):
    """
    Mark this shell to be destroyed at the next iteration of the global IOLoop instance.
    """
    self.remove_at_next_iteration = True

  def get_cached_output(self, chunk_id):
    """
    The chunk ID is not the latest one, so that chunk of output has already been generated and is
    stored in the read buffer. So let's fetch it from there.
    """
    start_pos = self._output_chunk_info[chunk_id][0]
    end_pos = self._output_chunk_info[-1][1]
    old_pos = self._read_buffer.tell()
    bytes_to_read = end_pos - start_pos
    self._read_buffer.seek(start_pos)
    output = self._read_buffer.read(bytes_to_read)
    self._read_buffer.seek(old_pos)
    return output
  
  def output_request_received(self, hue_instance_id, chunk_id):
    """
    If chunk_id represents an old chunk id, which already has output, returns all cached output 
    since that chunk and the next chunk ID in a dictionary. Chunks are coalesced if the chunk id
    is more than 1 chunk old, so the returned next chunk ID is not guaranteed to be 1 greater than
    the one passed in.  If the chunk ID is the latest one, adds listeners and returns None.
    """
    self.time_received = time.time()
    if chunk_id >= len(self._output_chunk_info):
      self.enable_read_callback()
      self._output_connection_ids.add(hue_instance_id)
      return None
    cached_output = self.get_cached_output(chunk_id)
    return { constants.ALIVE: True, constants.OUTPUT: cached_output,
    constants.MORE_OUTPUT_AVAILABLE: True, constants.NEXT_CHUNK_ID: len(self._output_chunk_info) }

  def _output_connection_ids_to_list(self):
    """
    Converts the set of output connection Hue instance IDs to a list of IDs. Destructively modifies
    the set.
    """
    retval = []
    while True:
      try:
        next_item = self._output_connection_ids.pop()
      except KeyError:
        break
      else:
        retval.append(next_item)
    return retval
  
  def destroy(self):
    """
    Called during iterations of _handle_periodic in the global IOLoop. Removes the appropriate
    handlers from the IOLoop, does some cleanup, and then kills the subprocess.
    """
    self.disable_read_callback()
    self.disable_write_callback()

    self._write_buffer.close()
    self._read_buffer.close()

    try:
      LOG.debug("Sending SIGKILL to process with PID %d" % (self._subprocess.pid,))
      os.kill(self._subprocess.pid, signal.SIGKILL)
      # We could try figure out which exit statuses are fine and which ones are errors.
      # But that might be difficult to do since os.wait might block.
    except OSError:
      pass # This means the subprocess was already killed, which happens if the command was "quit"

    output_conn_id_list = self._output_connection_ids_to_list()
    output_connections = self._smanager.output_connections_by_ids(output_conn_id_list)
    for output_connection in output_connections:
      utils.write(output_connection, { self.shell_id : { constants.SHELL_KILLED : True }}, True)

    while len(self._prompt_connections):
      prompt_connection = self._prompt_connections.pop()
      utils.write(prompt_connection, { constants.SHELL_KILLED : True }, True)

  def _read_child_output(self):
    """
    Reads up to constants.OS_READ_AMOUNT bytes from the child subprocess's stdout. Returns a tuple
    of (output, more_available). The second parameter indicates whether more output might be
    obtained by another call to _read_child_output.
    """
    ofd = self._ofd
    try:
      next_output = os.read(ofd, constants.OS_READ_AMOUNT)
      old_pos = self._read_buffer.tell()
      self._read_buffer.write(next_output)
      self._read_buffer.seek(old_pos)
    except OSError, e: # No more output at all
      if e.errno == errno.EINTR:
        pass
      elif e.errno != errno.EAGAIN:
        format_str = "Encountered error while reading from process with PID %d : %s"
        LOG.error( format_str % (self._subprocess.pid, e))
        self.mark_for_cleanup()
    result = self._read_buffer.read()
    self._output_chunk_info.append(( old_pos, self._read_buffer.tell() ))
    more_available = len(result) >= constants.OS_READ_AMOUNT
    next_chunk_id = len(self._output_chunk_info)
    return (result, more_available, next_chunk_id)

  def _child_readable(self, fd, events):
    """
    Called by the IOLoop when the child process's output fd has data that can be read. The data is
    read out and then written back over the output connections for this shell.
    """
    LOG.debug("child_readable")
    total_output, more_available, next_chunk_id = self._read_child_output()

    # If this is the last output from the shell, let's tell the JavaScript that.
    if self._subprocess.poll() == None:
      status = constants.ALIVE
    else:
      status = constants.EXITED
      self.last_output_sent = True

    output_conn_id_list = self._output_connection_ids_to_list()
    output_connections = self._smanager.output_connections_by_ids(output_conn_id_list)
    result = { self.shell_id: { status: True, constants.OUTPUT: total_output,
           constants.MORE_OUTPUT_AVAILABLE: more_available, constants.NEXT_CHUNK_ID: next_chunk_id}}
    try:
      for output_connection in output_connections:
        utils.write(output_connection, result, True)
    finally:
      self.disable_read_callback()

  def _append_to_write_buffer(self, command):
    """
    Append the received command, with an extra newline, to the write buffer. This buffer is used
    when the child becomes readable to send commands to the child subprocess.
    """
    self._write_buffer.seek(len(self._write_buffer.getvalue()))
    self._write_buffer.write("%s\n" % (command,))
    self._write_buffer.seek(0)

  def _read_from_write_buffer(self):
    """
    Read and return the contents of the write buffer.
    """
    contents = self._write_buffer.read()
    self._write_buffer.seek(0)
    return contents

  def _advance_write_buffer(self, num_bytes):
    """
    Advance the current position in the write buffer by num_bytes bytes.
    """
    # TODO: Replace this system with a list of cStringIO objects so that
    # it's more efficient. We should do this if this seems to be copying
    # a lot of memory around.
    self._write_buffer.seek(num_bytes)
    new_value = self._write_buffer.read()
    self._write_buffer.truncate(0)
    self._write_buffer.write(new_value)
    self._write_buffer.seek(0)

  def _child_writable(self, fd, events):
    """
    Called by the global IOLoop instance when a child subprocess's input file descriptor becomes 
    available for writing. This is the point at which we send the OK back to the client so that the
    prompt can become available in the browser window.
    """
    LOG.debug("child_writable")
    buffer_contents = self._read_from_write_buffer()
    if buffer_contents != "":
      try:
        bytes_written = os.write(fd, buffer_contents)
        self._advance_write_buffer(bytes_written)
      except OSError, e:
        if e.errno == errno.EINTR:
          return
        elif e.errno != errno.EAGAIN:
          format_str = "Encountered error while writing to process with PID %d:%s"
          LOG.error(format_str % (self._subprocess.pid, e))
          self.mark_for_cleanup()
          return
      else:
        return

    # We could try to figure out how many bytes were written, and then immediately do the stuff 
    # below, but having this code and the code above be mutually exclusive makes the code cleaner
    # and less error-prone (and not much more CPU-intensive).

    self.disable_write_callback()
    # We have prompt connections to acknowledge that we can receive more stuff. Let's do that.
    while len(self._prompt_connections):
      prompt_connection = self._prompt_connections.pop()
      utils.write(prompt_connection, { constants.SUCCESS : True }, True)

class ShellManager(object):
  """
  The class that manages the relationship between requests and shell subprocesses.
  """
  def __init__(self):
    self._shells = {} # The keys are (username, shell_id) tuples
    self._meta = {} # The keys here are usernames
    self._output_connections = {} # Keys are Hue Instance IDs, values are wrapped connections
    self._io_loop = tornado.ioloop.IOLoop.instance()
    self._periodic_callback = tornado.ioloop.PeriodicCallback(self._handle_periodic, 1000)
    self._periodic_callback.start()

  @classmethod
  def global_instance(cls):
    """
    Similar to IOLoop's instance() classmethod. This provides an easy way for all objects to have
    a reference to the same ShellManager object without having to pass around a reference to it.
    """
    if not hasattr(cls, "_global_instance"):
      cls._global_instance = cls()
    return cls._global_instance

  def _cleanup_shell(self, key):
    """
    Clean up the shell corresponding to the specified key. Calls the destroy method on the given
    shell subprocess and then changes the metadata to keep it accurate.
    """
    shell_instance = self._shells[key]
    shell_instance.destroy()
    self._shells.pop(key)
    username = key[0]
    self._meta[username].decrement_count()

  def _handle_timeouts(self):
    """
    Called every iteration of the global IOLoop by the global ShellManager. This lets us time some
    old output connections out by writing a "Keep-Alive" equivalent.
    """
    currtime = time.time()
    keys_to_pop = []
    for hue_instance_id, connection in self._output_connections.iteritems():
      difftime = currtime - connection.time_received
      if difftime >= constants.BROWSER_REQUEST_TIMEOUT:
        keys_to_pop.append(hue_instance_id)
    
    for key in keys_to_pop:
      connection = self._output_connections.pop(key)
      utils.write(connection.handler, { constants.PERIODIC_RESPONSE : True }, True)

  def _handle_periodic(self):
    """
    Called at every IOLoop iteration. Kills the necessary shells and responds to the outstanding
    requests which will soon time out with "keep-alive" type messages.
    """
    LOG.debug("Entering _handle_periodic")
    try:
      keys_to_pop = []
      current_time = time.time()
      for key, shell in self._shells.iteritems():
        if shell.last_output_sent or shell.remove_at_next_iteration:
          keys_to_pop.append(key)
        else:
          difftime = current_time - shell.time_received
          if difftime >= constants.SHELL_TIMEOUT:
            keys_to_pop.append(key)
      for key in keys_to_pop:
        self._cleanup_shell(key)
      self._handle_timeouts()
    finally:
      LOG.debug("Leaving _handle_periodic")

  def _get_command_from_key(self, key_name):
    """
    Attempts to determine the command to run from the key_name passed in. For example, 
    "flume_shell" becomes "flume shell" and "pig_shell" becomes "pig -l /dev/null".
    """
    key_name = str(key_name)
    command = None
    try:
      command = getattr(shell.conf, key_name.upper()) # The short-circuit
      command = command.get()
    except AttributeError:
      shell_types = shell.conf.SHELL_TYPES.get() # If it fails, look through all of SHELL_TYPES
      shell_types = shell_types.split(",")
      shell_types = [getattr(shell.conf, item.strip()) for item in shell_types]
      for item in shell_types:
        if item.config.key == key_name:
          command = item.config.default
          break
    return command
  
  def try_create(self, username, key_name, connection):
    """
    Attemps to create a new shell subprocess for the given user. Writes the appropriate failure or
    success response to the client.
    """
    command = self._get_command_from_key(key_name)
    if command is None:
      utils.write(connection, { constants.SHELL_CREATE_FAILED : True })
      return

    if not username in self._meta:
      self._meta[username] = utils.UserMetadata(username)
    user_metadata = self._meta[username] 
    shell_id = user_metadata.get_next_id()
    user_metadata.increment_count()
    try:
      LOG.debug("Trying to create a shell for user %s" % (username,))
      shell_instance = Shell(command, shell_id)
    except (OSError, ValueError), exc:
      LOG.error("Could not create shell : %s" % (exc,))
      utils.write(connection, { constants.SHELL_CREATE_FAILED : True })
      return

    LOG.debug("Shell successfully created")
    self._shells[(username, shell_id)] = shell_instance
    shell_instance.shell_id = shell_id
    utils.write(connection, { constants.SUCCESS : True, constants.SHELL_ID : shell_id })

  def command_received(self, username, shell_id, command, connection):
    """
    Called when a command is received from the client. Sends the command to the appropriate
    Shell instance.
    """
    shell = self._shells.get((username, shell_id))
    if not shell:
      utils.write(connection, { constants.NO_SHELL_EXISTS : True }, True)
      return
    shell.command_received(command, connection)

  def output_request_received(self, username, hue_instance_id, shell_pairs, connection):
    """
    Called when an output request is received from the client. Sends the request to the appropriate
    shell instances.
    """
    total_cached_output = {}
    for shell_id, chunk_id in shell_pairs:
      shell = self._shells.get((username, shell_id))
      if shell:
        result = shell.output_request_received(hue_instance_id, chunk_id)
        if result:
          total_cached_output[shell_id] = cached_output
      else:
        LOG.warn("User '%s' has no shell with ID '%s'" % (username, shell_id))
        total_cached_output[shell_id] = { constants.NO_SHELL_EXISTS: True }

    if total_cached_output:
      utils.write(connection, total_cached_output, True)
    else:
      if hue_instance_id in self._output_connections:
        LOG.warn("Hue Instance ID '%s' already has an output connection, replacing...")
      self._output_connections[hue_instance_id] = TimestampedConnection(connection)

  def output_connections_by_ids(self, ids):
    retval = []
    for item in ids:
      try:
        next_connection = self._output_connections.pop(item)
      except KeyError:
        pass # Some other shell got to it, or it expired, etc. No need to worry
      else:
        retval.append(next_connection.handler)
    return retval

  def kill_shell(self, username, shell_id):
    """
    Called when the user closes the JFrame in Hue. Marks the appropriate shell for cleanup on the
    next IOLoop iteration.
    """
    shell = self._shells.get((username, shell_id))
    if not shell:
      LOG.debug("User '%s' has no shell with ID '%s'" % (username, shell_id))
      return
    shell.mark_for_cleanup()

  def get_shell_types(self):
    """
    Returns a list of the shell types available. Each shell type available is a dictionary with keys
    constants.NICE_NAME and constants.KEY_NAME
    """
    if not hasattr(self, "_cached_shell_types"):
      shell_types = shell.conf.SHELL_TYPES.get()
      shell_types = shell_types.split(",")
      shell_types = [getattr(shell.conf, item.strip()) for item in shell_types]
      shell_types = [{constants.NICE_NAME: item.config.help, 
                      constants.KEY_NAME: item.config.key} for item in shell_types]
      self._cached_shell_types = shell_types
    return self._cached_shell_types

  def handle_shell_types_request(self, username, connection):
    """
    Responds with the shell types available, if the user is allowed to have any more shells open.
    Otherwise responds with 'No more shells' message.
    """
    if not hasattr(self, "_cached_shell_types_response"):
      self._cached_shell_types_response = { constants.SUCCESS: True, 
                                   constants.SHELL_TYPES: self.get_shell_types() }
    utils.write(connection, self._cached_shell_types_response)

  def get_connection_by_hue_id(self, hue_instance_id):
    """
    Returns the output connection uniquely identified by the given Hue instance ID.
    """
    return self._output_connections.pop(hue_instance_id, None)

  def get_previous_output(self, username, shell_id):
    """
    Called when the Hue session is restored. Get the outputs that we have previously written out to
    the client as one big string.
    """
    shell = self._shells.get((username, shell_id))
    if not shell:
      return { constants.SHELL_KILLED : True}
    output, next_cid = shell.get_previous_output()
    return {constants.SUCCESS: True, constants.OUTPUT: output, constants.NEXT_CHUNK_ID: next_cid}

  def add_to_output(self, username, hue_instance_id, shell_pairs, connection):
    total_cached_output = {}
    for shell_id, chunk_id in shell_pairs:
      shell = self._shells.get((username, shell_id))
      if shell:
        result = shell.output_request_received(hue_instance_id, chunk_id)
        if result:
          total_cached_output[shell_id] = cached_output
      else:
        LOG.warn("User '%s' has no shell with ID '%s'" % (username, shell_id))

    if total_cached_output:
      output_connection = self.output_connections_by_ids([hue_instance_id])
      if output_connection:
        output_connection = output_connection[0]
        utils.write(output_connection, total_cached_output, True)

    utils.write(connection, { constants.SUCCESS: True })
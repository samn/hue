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
A module that provides the handlers for the Tornado component of the shell app.
"""

import shell.constants as constants
import desktop.lib.tornado_utils as tornado_utils
import shell.utils as utils
import shell.shellmanager as shellmanager
import tornado
import logging

LOG = logging.getLogger(__name__)

class RetrieveOutputHandler(tornado_utils.MiddlewareHandler):
  """
  Registers an output connection for the next available output from the subprocesses for the
  requesting user. Denies access to unauthenticated users and users without existing remote shells.
  """
  @tornado.web.asynchronous
  def post(self):
    if self.deny_hue_access:
      tornado_utils.write(self, {constants.NOT_LOGGED_IN: True}, True)
      return

    username = self.django_style_request.user.username
    try:
      hue_instance_id = self.request.headers.get_list(constants.HUE_INSTANCE_ID)[0]
    except IndexError:
      # No Hue instance ID, which is possible if they've had Hue open forever and the server
      # was upgraded in the meantime.
      tornado_utils.write(self, {constants.RESTART_HUE: True}, True)
      return
    smanager = shellmanager.ShellManager.global_instance()
    shell_pairs = utils.parse_shell_pairs(self)
    smanager.output_request_received(username, hue_instance_id, shell_pairs, self)

class AddToOutputHandler(tornado_utils.MiddlewareHandler):
  """
  Adds the shell_id passed in to the shared output connection for the given Hue client.
  """
  def post(self):
    if self.deny_hue_access:
      tornado_utils.write(self, { constants.NOT_LOGGED_IN: True })
      return

    username = self.django_style_request.user.username
    try:
      hue_instance_id = self.request.headers.get_list(constants.HUE_INSTANCE_ID)[0]
    except IndexError:
      # No Hue instance ID, so tell the user to restart Hue (i.e. refresh the browser window).
      tornado_utils.write(self, {constants.RESTART_HUE: True}, True)
      return
    smanager = shellmanager.ShellManager.global_instance()
    shell_pairs = utils.parse_shell_pairs(self)
    smanager.add_to_output(username, hue_instance_id, shell_pairs, self)

class GetShellTypesHandler(tornado_utils.MiddlewareHandler):
  """
  Returns a JS object enumerating the types of shells available on the server.
  """
  def get(self):
    if self.deny_hue_access:
      tornado_utils.write(self, { constants.NOT_LOGGED_IN: True })
      return
    shellmanager.ShellManager.global_instance().handle_shell_types_request(self)

class KillShellHandler(tornado_utils.MiddlewareHandler):
  """
  Tells the shell manager to kill the subprocess for this shell.
  """
  def post(self):
    if self.deny_hue_access:
      tornado_utils.write(self, { constants.NOT_LOGGED_IN: True })
      return

    shell_id = self.get_argument(constants.SHELL_ID, "")
    smanager = shellmanager.ShellManager.global_instance()
    smanager.kill_shell(self.django_style_request.user.username, shell_id)
    tornado_utils.write(self, "")

class CreateHandler(tornado_utils.MiddlewareHandler):
  """
  Attempts to create a subprocess for the requesting user. Denies access to users who are not logged
  in, and communicates whether or not the creation of the subprocess was successful.
  """
  def post(self):
    if self.deny_hue_access:
      tornado_utils.write(self, { constants.NOT_LOGGED_IN: True })
      return

    smanager = shellmanager.ShellManager.global_instance()
    key_name = self.get_argument(constants.KEY_NAME, "") # The key_name specifies what type of shell
    smanager.try_create(self.django_style_request.user.username, key_name, self)

class ProcessCommandHandler(tornado_utils.MiddlewareHandler):
  """
  Attempts to send the the specified command to the subprocess for the requesting user. Denies
  access to unauthenticated users and handles users without shells existing for them.
  """
  @tornado.web.asynchronous
  def post(self):
    if self.deny_hue_access:
      tornado_utils.write(self, {constants.NOT_LOGGED_IN: True}, True)
      return

    command = self.get_argument(constants.COMMAND, "", strip=False)
    LOG.debug("Command is : \"%s\"" % (command,))
    shell_id = self.get_argument(constants.SHELL_ID, "")
    smanager = shellmanager.ShellManager.global_instance()
    smanager.command_received(self.django_style_request.user.username, shell_id, command, self)

class RestoreShellHandler(tornado_utils.MiddlewareHandler):
  """
  Retrieves previous output for the given shell. This is done when we restore a Hue session. Denies
  access to unauthenticated users and users without existing remote shells.
  """
  def post(self):
    if self.deny_hue_access:
      tornado_utils.write(self, {constants.NOT_LOGGED_IN: True})
      return

    shell_id = self.get_argument(constants.SHELL_ID, "")
    smanager = shellmanager.ShellManager.global_instance()
    result = smanager.get_previous_output(self.django_style_request.user.username, shell_id)
    tornado_utils.write(self, result)
#!/usr/bin/env python
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

from hadoop.cluster import get_all_hdfs
from desktop import conf
from hadoop.fs import LocalSubFileSystem
from desktop.lib.apputil import has_hadoop

_filesystems = None

def _init_filesystems():
  """Initialize the module-scoped filesystem dictionary."""
  global _filesystems
  if _filesystems is not None:
    return
  _filesystems = {}

  if has_hadoop():
    # Load HDFSes
    _filesystems.update(get_all_hdfs())

  # Load local
  for identifier in conf.LOCAL_FILESYSTEMS.keys():
    local_fs = LocalSubFileSystem(
        conf.LOCAL_FILESYSTEMS[identifier].PATH.get())
    if identifier in _filesystems: 
      raise Exception(("Filesystem '%s' configured twice. First is " +
        "%s, second is local FS %s") % (identifier, _filesystems[identifier], local_fs))
    _filesystems[identifier] = local_fs

def get_filesystem(name):
  """Return the filesystem with the given name. If the filesystem is not defined,
  raises KeyError"""
  _init_filesystems()
  return _filesystems[name]

def get_default_hdfs():
  """
  Return the (name, fs) for the default hdfs.
  Return (None, None) if no hdfs cluster configured
  """
  _init_filesystems()
  for name, fs in _filesystems.iteritems():
    # Return the first HDFS encountered
    if fs.uri.startswith('hdfs'):
      return name, fs
  return None, None

def reset():
  """
  reset() -- Forget all cached filesystems and go to a pristine state.
  """
  global _filesystems
  _filesystems = None

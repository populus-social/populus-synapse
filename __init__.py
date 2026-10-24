# SPDX-License-Identifier: AGPL-3.0-only
"""The populus Synapse module (AGPL-3.0, see LICENSE in this directory).

One in-process module that loads into our Synapse image. It is a derivative
work of Synapse because it imports synapse.module_api. Synapse itself is
not patched.
"""

from populus_synapse.storage_allowance import StorageAllowanceModule

__all__ = ["StorageAllowanceModule"]

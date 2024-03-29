# Copyright (C) 2013 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Handles all requests relating to transferring ownership of volumes.
"""


import hashlib
import hmac
import os

from oslo.config import cfg

from cinder.db import base
from cinder import exception
from cinder.openstack.common import excutils
from cinder.openstack.common.gettextutils import _
from cinder.openstack.common import log as logging
from cinder import quota
from cinder.volume import api as volume_api


volume_transfer_opts = [
    cfg.IntOpt('volume_transfer_salt_length', default=8,
               help='The number of characters in the salt.'),
    cfg.IntOpt('volume_transfer_key_length', default=16,
               help='The number of characters in the '
               'autogenerated auth key.'), ]

CONF = cfg.CONF
CONF.register_opts(volume_transfer_opts)

LOG = logging.getLogger(__name__)
QUOTAS = quota.QUOTAS


class API(base.Base):
    """API for interacting volume transfers."""

    def __init__(self, db_driver=None):
        self.volume_api = volume_api.API()
        super(API, self).__init__(db_driver)

    def get(self, context, transfer_id):
        rv = self.db.transfer_get(context, transfer_id)
        return dict(rv.iteritems())

    def delete(self, context, transfer_id):
        """Make the RPC call to delete a volume transfer."""
        volume_api.check_policy(context, 'delete_transfer')
        transfer = self.db.transfer_get(context, transfer_id)

        volume_ref = self.db.volume_get(context, transfer.volume_id)
        if volume_ref['status'] != 'awaiting-transfer':
            msg = _("Volume in unexpected state")
            LOG.error(msg)
        self.db.transfer_destroy(context, transfer_id)

    def get_all(self, context, filters=None):
        filters = filters or {}
        volume_api.check_policy(context, 'get_all_transfers')
        if context.is_admin and 'all_tenants' in filters:
            transfers = self.db.transfer_get_all(context)
        else:
            transfers = self.db.transfer_get_all_by_project(context,
                                                            context.project_id)
        return transfers

    def _get_random_string(self, length):
        """Get a random hex string of the specified length."""
        rndstr = ""

        # Note that the string returned by this function must contain only
        # characters that the recipient can enter on their keyboard. The
        # function ssh224().hexdigit() achieves this by generating a hash
        # which will only contain hexidecimal digits.
        while len(rndstr) < length:
            rndstr += hashlib.sha224(os.urandom(255)).hexdigest()

        return rndstr[0:length]

    def _get_crypt_hash(self, salt, auth_key):
        """Generate a random hash based on the salt and the auth key."""
        return hmac.new(str(salt),
                        str(auth_key),
                        hashlib.sha1).hexdigest()

    def create(self, context, volume_id, display_name):
        """Creates an entry in the transfers table."""
        volume_api.check_policy(context, 'create_transfer')
        LOG.info("Generating transfer record for volume %s" % volume_id)
        volume_ref = self.db.volume_get(context, volume_id)
        if volume_ref['status'] != "available":
            raise exception.InvalidVolume(reason=_("status must be available"))

        # The salt is just a short random string.
        salt = self._get_random_string(CONF.volume_transfer_salt_length)
        auth_key = self._get_random_string(CONF.volume_transfer_key_length)
        crypt_hash = self._get_crypt_hash(salt, auth_key)

        # TODO(ollie): Transfer expiry needs to be implemented.
        transfer_rec = {'volume_id': volume_id,
                        'display_name': display_name,
                        'salt': salt,
                        'crypt_hash': crypt_hash,
                        'expires_at': None}

        try:
            transfer = self.db.transfer_create(context, transfer_rec)
        except Exception:
            LOG.error(_("Failed to create transfer record for %s") % volume_id)
            raise
        return {'id': transfer['id'],
                'volume_id': transfer['volume_id'],
                'display_name': transfer['display_name'],
                'auth_key': auth_key,
                'created_at': transfer['created_at']}

    def accept(self, context, transfer_id, auth_key):
        """Accept a volume that has been offered for transfer."""
        # We must use an elevated context to see the volume that is still
        # owned by the donor.
        volume_api.check_policy(context, 'accept_transfer')
        transfer = self.db.transfer_get(context.elevated(), transfer_id)

        crypt_hash = self._get_crypt_hash(transfer['salt'], auth_key)
        if crypt_hash != transfer['crypt_hash']:
            msg = (_("Attempt to transfer %s with invalid auth key.") %
                   transfer_id)
            LOG.error(msg)
            raise exception.InvalidAuthKey(reason=msg)

        volume_id = transfer['volume_id']
        vol_ref = self.db.volume_get(context.elevated(), volume_id)

        try:
            reservations = QUOTAS.reserve(context, volumes=1,
                                          gigabytes=vol_ref['size'])
        except exception.OverQuota as e:
            overs = e.kwargs['overs']
            usages = e.kwargs['usages']
            quotas = e.kwargs['quotas']

            def _consumed(name):
                return (usages[name]['reserved'] + usages[name]['in_use'])

            if 'gigabytes' in overs:
                msg = _("Quota exceeded for %(s_pid)s, tried to create "
                        "%(s_size)sG volume (%(d_consumed)dG of %(d_quota)dG "
                        "already consumed)")
                LOG.warn(msg % {'s_pid': context.project_id,
                                's_size': vol_ref['size'],
                                'd_consumed': _consumed('gigabytes'),
                                'd_quota': quotas['gigabytes']})
                raise exception.VolumeSizeExceedsAvailableQuota(
                    requested=vol_ref['size'],
                    consumed=_consumed('gigabytes'),
                    quota=quotas['gigabytes'])
            elif 'volumes' in overs:
                msg = _("Quota exceeded for %(s_pid)s, tried to create "
                        "volume (%(d_consumed)d volumes "
                        "already consumed)")
                LOG.warn(msg % {'s_pid': context.project_id,
                                'd_consumed': _consumed('volumes')})
                raise exception.VolumeLimitExceeded(allowed=quotas['volumes'])
        try:
            donor_id = vol_ref['project_id']
            donor_reservations = QUOTAS.reserve(context.elevated(),
                                                project_id=donor_id,
                                                volumes=-1,
                                                gigabytes=-vol_ref['size'])
        except Exception:
            donor_reservations = None
            LOG.exception(_("Failed to update quota donating volume"
                            "transfer id %s") % transfer_id)

        try:
            # Transfer ownership of the volume now, must use an elevated
            # context.
            self.volume_api.accept_transfer(context,
                                            vol_ref,
                                            context.user_id,
                                            context.project_id)
            self.db.transfer_accept(context.elevated(),
                                    transfer_id,
                                    context.user_id,
                                    context.project_id)
            QUOTAS.commit(context, reservations)
            if donor_reservations:
                QUOTAS.commit(context, donor_reservations, project_id=donor_id)
            LOG.info(_("Volume %s has been transferred.") % volume_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                QUOTAS.rollback(context, reservations)
                if donor_reservations:
                    QUOTAS.rollback(context, donor_reservations,
                                    project_id=donor_id)

        vol_ref = self.db.volume_get(context, volume_id)
        return {'id': transfer_id,
                'display_name': transfer['display_name'],
                'volume_id': vol_ref['id']}

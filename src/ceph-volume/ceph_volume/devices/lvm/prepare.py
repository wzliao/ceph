from __future__ import print_function
import json
import uuid
from textwrap import dedent
from ceph_volume.util import prepare as prepare_utils
from ceph_volume.util import system, disk
from ceph_volume import conf, decorators, terminal
from ceph_volume.api import lvm as api
from .common import prepare_parser


def prepare_filestore(device, journal, secrets, id_=None, fsid=None):
    """
    :param device: The name of the logical volume to work with
    :param journal: similar to device but can also be a regular/plain disk
    :param secrets: A dict with the secrets needed to create the osd (e.g. cephx)
    :param id_: The OSD id
    :param fsid: The OSD fsid, also known as the OSD UUID
    """
    cephx_secret = secrets.get('cephx_secret', prepare_utils.create_key())
    json_secrets = json.dumps(secrets)

    # allow re-using an existing fsid, in case prepare failed
    fsid = fsid or system.generate_uuid()
    # allow re-using an id, in case a prepare failed
    osd_id = id_ or prepare_utils.create_id(fsid, json_secrets)
    # create the directory
    prepare_utils.create_osd_path(osd_id)
    # format the device
    prepare_utils.format_device(device)
    # mount the data device
    prepare_utils.mount_osd(device, osd_id)
    # symlink the journal
    prepare_utils.link_journal(journal, osd_id)
    # get the latest monmap
    prepare_utils.get_monmap(osd_id)
    # prepare the osd filesystem
    prepare_utils.osd_mkfs_filestore(osd_id, fsid)
    # write the OSD keyring if it doesn't exist already
    prepare_utils.write_keyring(osd_id, cephx_secret)


def prepare_bluestore(block, wal, db, secrets, id_=None, fsid=None):
    """
    :param block: The name of the logical volume for the bluestore data
    :param wal: a regular/plain disk or logical volume, to be used for block.wal
    :param db: a regular/plain disk or logical volume, to be used for block.db
    :param secrets: A dict with the secrets needed to create the osd (e.g. cephx)
    :param id_: The OSD id
    :param fsid: The OSD fsid, also known as the OSD UUID
    """
    cephx_secret = secrets.get('cephx_secret', prepare_utils.create_key())
    json_secrets = json.dumps(secrets)

    # allow re-using an existing fsid, in case prepare failed
    fsid = fsid or system.generate_uuid()
    # allow re-using an id, in case a prepare failed
    osd_id = id_ or prepare_utils.create_id(fsid, json_secrets)
    # create the directory
    prepare_utils.create_osd_path(osd_id, tmpfs=True)
    # symlink the block, wal, and db
    prepare_utils.link_block(block, osd_id)
    if wal:
        prepare_utils.link_wal(wal, osd_id)
    if db:
        prepare_utils.link_db(db, osd_id)
    # get the latest monmap
    prepare_utils.get_monmap(osd_id)
    # write the OSD keyring if it doesn't exist already
    prepare_utils.write_keyring(osd_id, cephx_secret)
    # prepare the osd filesystem
    prepare_utils.osd_mkfs_bluestore(osd_id, fsid, keyring=cephx_secret)


class Prepare(object):

    help = 'Format an LVM device and associate it with an OSD'

    def __init__(self, argv):
        self.argv = argv

    def get_ptuuid(self, argument):
        uuid = disk.get_partuuid(argument)
        if not uuid:
            terminal.error('blkid could not detect a PARTUUID for device: %s' % argument)
            raise RuntimeError('unable to use device')
        return uuid

    def get_lv(self, argument):
        """
        Perform some parsing of the command-line value so that the process
        can determine correctly if it got a device path or an lv.

        :param argument: The command-line value that will need to be split to
                         retrieve the actual lv
        """
        try:
            vg_name, lv_name = argument.split('/')
        except (ValueError, AttributeError):
            return None
        return api.get_lv(lv_name=lv_name, vg_name=vg_name)

    def setup_device(self, device_type, device_name, tags):
        """
        Check if ``device`` is an lv, if so, set the tags, making sure to
        update the tags with the lv_uuid and lv_path which the incoming tags
        will not have.

        If the device is not a logical volume, then retrieve the partition UUID
        by querying ``blkid``
        """
        if device_name is None:
            return '', '', tags
        tags['ceph.type'] = device_type
        lv = self.get_lv(device_name)
        if lv:
            uuid = lv.lv_uuid
            path = lv.lv_path
            tags['ceph.%s_uuid' % device_type] = uuid
            tags['ceph.%s_device' % device_type] = path
            lv.set_tags(tags)
            return path, uuid, tags
        # otherwise assume this is a regular disk partition
        return device_name, self.get_ptuuid(device_name), tags

    @decorators.needs_root
    def prepare(self, args):
        # FIXME we don't allow re-using a keyring, we always generate one for the
        # OSD, this needs to be fixed. This could either be a file (!) or a string
        # (!!) or some flags that we would need to compound into a dict so that we
        # can convert to JSON (!!!)
        secrets = {'cephx_secret': prepare_utils.create_key()}

        cluster_fsid = conf.ceph.get('global', 'fsid')
        osd_fsid = args.osd_fsid or system.generate_uuid()
        # allow re-using an id, in case a prepare failed
        osd_id = args.osd_id or prepare_utils.create_id(osd_fsid, json.dumps(secrets))
        if args.filestore:
            if not args.journal:
                raise RuntimeError('--journal is required when using --filestore')

            data_lv = self.get_lv(args.data)
            if not data_lv:
                raise RuntimeError('no data logical volume found with: %s' % args.data)

            tags = {
                'ceph.osd_fsid': osd_fsid,
                'ceph.osd_id': osd_id,
                'ceph.cluster_fsid': cluster_fsid,
                'ceph.data_device': data_lv.lv_path,
                'ceph.data_uuid': data_lv.lv_uuid,
            }

            journal_device, journal_uuid, tags = self.setup_device('journal', args.journal, tags)

            data_lv.set_tags(tags)

            prepare_filestore(
                data_lv.lv_path,
                journal_device,
                secrets,
                id_=osd_id,
                fsid=osd_fsid,
            )
        elif args.bluestore:
            block_lv = self.get_lv(args.data)
            if not block_lv:
                if disk.is_partition(args.data) or disk.is_device(args.data):
                    # we must create a vg, and then a single lv
                    vg_name = "ceph-%s" % cluster_fsid
                    if api.get_vg(vg_name=vg_name):
                        # means we already have a group for this, make a different one
                        # XXX this could end up being annoying for an operator, maybe?
                        vg_name = "ceph-%s" % str(uuid.uuid4())
                    api.create_vg(vg_name, args.data)
                    block_name = "osd-block-%s" % osd_fsid
                    block_lv = api.create_lv(
                        block_name,
                        vg_name,  # the volume group
                        tags={'ceph.type': 'block'})
                else:
                    error = [
                        'Cannot use device (%s) for bluestore. ' % args.data,
                        'A vg/lv path or an existing device is needed'
                    ]
                    raise RuntimeError(' '.join(error))

            tags = {
                'ceph.osd_fsid': osd_fsid,
                'ceph.osd_id': osd_id,
                'ceph.cluster_fsid': cluster_fsid,
                'ceph.block_device': block_lv.lv_path,
                'ceph.block_uuid': block_lv.lv_uuid,
            }

            wal_device, wal_uuid, tags = self.setup_device('wal', args.block_wal, tags)
            db_device, db_uuid, tags = self.setup_device('db', args.block_db, tags)

            block_lv.set_tags(tags)

            prepare_bluestore(
                block_lv.lv_path,
                wal_device,
                db_device,
                secrets,
                id_=osd_id,
                fsid=osd_fsid,
            )

    def main(self):
        sub_command_help = dedent("""
        Prepare an OSD by assigning an ID and FSID, registering them with the
        cluster with an ID and FSID, formatting and mounting the volume, and
        finally by adding all the metadata to the logical volumes using LVM
        tags, so that it can later be discovered.

        Once the OSD is ready, an ad-hoc systemd unit will be enabled so that
        it can later get activated and the OSD daemon can get started.

        Example calls for supported scenarios:

        Filestore
        ---------

          Existing logical volume (lv) or device:

              ceph-volume lvm prepare --filestore --data {vg/lv} --journal /path/to/device

          Or:

              ceph-volume lvm prepare --filestore --data {vg/lv} --journal {vg/lv}

        Bluestore
        ---------

          Existing logical volume (lv):

              ceph-volume lvm prepare --bluestore --data {vg/lv}

          Existing block device, that will be made a group and logical volume:

              ceph-volume lvm prepare --bluestore --data /path/to/device

          Optionally, can consume db and wal devices or logical volumes:

              ceph-volume lvm prepare --bluestore --data {vg/lv} --block.wal {device} --block-db {vg/lv}
        """)
        parser = prepare_parser(
            prog='ceph-volume lvm prepare',
            description=sub_command_help,
        )
        if len(self.argv) == 0:
            print(sub_command_help)
            return
        args = parser.parse_args(self.argv)
        self.prepare(args)

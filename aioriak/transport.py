import os
import logging
os.environ['PYTHONASYNCIODEBUG'] = '1'
# logging.basicConfig(level=logging.DEBUG)

import asyncio
import struct
import riak_pb
from riak_pb import messages
from riak.transports.pbc import codec
from riak.content import RiakContent
from riak.util import decode_index_value


MAX_CHUNK_SIZE = 65536
MAX_CHUNK_SIZE = 1024

logger = logging.getLogger('aioriak.transport')

# Debug
import sys
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(formatter)
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)


async def create_transport(host='localhost', port=8087, loop=None):
    reader, writer = await asyncio.open_connection(
        host, port, loop=loop)
    conn = RiakPbcAsyncTransport(reader, writer, loop=loop)
    return conn


class AsyncPBStream:
    '''
    Used internally by RiakPbcAsyncTransport to implement streaming
    operations. Implements the async iterator interface.
    '''
    async def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopIteration


class RPBPacketParser:
    """ Riak protobuf packet parser."""
    HEADER_LENGTH = 4

    def __init__(self, reader, initial_data=bytearray(), loop=None):
        self._loop = loop or asyncio.get_event_loop()
        self._data = initial_data
        self._reader = reader
        self._header_parsed = False
        self._tail = bytearray()
        self._eof = False

    @property
    def tail(self):
        return self._tail

    def _parse_header(self):
        if self._header_parsed:
            return True
        if len(self._data) >= self.HEADER_LENGTH:
            self._msglen, = struct.unpack(
                '!i', self._data[:self.HEADER_LENGTH])
            if self._msglen > 8192:
                raise Exception('Wrong MESSAGE_LEN %d', self._msglen)
            self._header_parsed = True
        else:
            self._header_parsed = False
        return self._header_parsed

    def _parse_msg(self):
        self._msg = self._data[
            self.HEADER_LENGTH:self.HEADER_LENGTH + self._msglen]
        self.msg_code, = struct.unpack("B", self._msg[:1])
        if self.msg_code is messages.MSG_CODE_ERROR_RESP:
            logger.error('Riak error message reciever')
            raise Exception('Raik error', self._msg)
        elif self.msg_code in messages.MESSAGE_CLASSES:
            logger.debug('Normal message with code %d received', self.msg_code)
            self.msg = self._get_pb_msg(self.msg_code, self._msg[1:])
        else:
            logger.error('Unknown message received [%d]', self.msg_code)

    def _grow_tail(self):
        if len(self._data) > self._msglen + self.HEADER_LENGTH:
            self._tail = self._data[
                self.HEADER_LENGTH + self._msglen:]
        else:
            self._tail = bytearray()

    def _check_eof(self):
        if self._header_parsed and \
                len(self._data) >= self.HEADER_LENGTH + self._msglen:
            self._eof = True
        return self._eof

    def _get_pb_msg(self, code, msg):
        try:
            pbclass = messages.MESSAGE_CLASSES[code]
        except KeyError:
            pbclass = None

        if pbclass is None:
            return None
        pbo = pbclass()
        pbo.ParseFromString(bytes(msg))
        return pbo

    def at_eof(self):
        return self._eof

    async def get_pbo(self):
        if self._parse_header():
            if self._check_eof():
                self._parse_msg()
                self._grow_tail()
                return self.msg_code, self.msg

        while not self.at_eof():
            chunk = await self._reader.read(MAX_CHUNK_SIZE)
            self._data.extend(chunk)
            self._parse_header()
            if self._check_eof():
                self._parse_msg()
                self._grow_tail()
                return self.msg_code, self.msg


class RPBStreamParser:
    '''
    Riak protobuf stream packets parser
    This class is async generator with feed_data method
    and iterable packets on stream
    '''

    def __init__(self, reader, loop=None):
        self._loop = loop or asyncio.get_event_loop()
        self._reader = reader
        self._in_buf = bytearray()
        self.finished = False

    @property
    def tail(self):
        return self._in_buf

    async def __aiter__(self):
        return self

    async def __anext__(self):
        if self.finished:
            raise StopAsyncIteration
        msg_code, pbo = await self._fetch_pbo()
        if msg_code is not None:
            if pbo.done:
                self.finished = True
            return (msg_code, pbo)
        else:
            raise StopAsyncIteration

    async def _fetch_pbo(self):
        parser = RPBPacketParser(self._reader, self._in_buf, self._loop)
        code, pbo = await parser.get_pbo()
        self._in_buf = parser.tail
        return code, pbo


class RiakPbcAsyncTransport:
    ParserClass = RPBPacketParser
    StreamParserClass = RPBStreamParser

    def __init__(self, reader, writer, loop=None):
        self._loop = loop or asyncio.get_event_loop()
        self._writer = writer
        self._reader = reader
        self._parser = None

    def _encode_bucket_props(self, props, msg):
        """
        Encodes a dict of bucket properties into the protobuf message.
        :param props: bucket properties
        :type props: dict
        :param msg: the protobuf message to fill
        :type msg: riak_pb.RpbSetBucketReq
        """
        for prop in codec.NORMAL_PROPS:
            if prop in props and props[prop] is not None:
                if isinstance(props[prop], str):
                    prop_value = props[prop].encode()
                else:
                    prop_value = props[prop]
                setattr(msg.props, prop, prop_value)
        for prop in codec.COMMIT_HOOK_PROPS:
            if prop in props:
                setattr(msg.props, 'has_' + prop, True)
                self._encode_hooklist(props[prop], getattr(msg.props, prop))
        for prop in codec.MODFUN_PROPS:
            if prop in props and props[prop] is not None:
                self._encode_modfun(props[prop], getattr(msg.props, prop))
        for prop in codec.QUORUM_PROPS:
            if prop in props and props[prop] not in (None, 'default'):
                value = self._encode_quorum(props[prop])
                if value is not None:
                    setattr(msg.props, prop, value)
        if 'repl' in props:
            msg.props.repl = codec.REPL_TO_PY[props['repl']]

        return msg

    def _encode_message(self, msg_code, msg=None):
        if msg is None:
            return struct.pack("!iB", 1, msg_code)
        msgstr = msg.SerializeToString()
        slen = len(msgstr)
        hdr = struct.pack("!iB", 1 + slen, msg_code)
        return hdr + msgstr

    @classmethod
    def _decode_pbo(cls, message):
        result = {}
        for key, value in message.ListFields():
            result[key.name] = value
        return result

    def _decode_dt_fetch(self, resp):
        dtype = codec.DT_FETCH_TYPES.get(resp.type)
        if dtype is None:
            raise ValueError("Unknown datatype on wire: {}".format(resp.type))

        value = self._decode_dt_value(dtype, resp.value)

        if resp.HasField('context'):
            context = resp.context[:]
        else:
            context = None

        return dtype, value, context

    def _decode_dt_value(self, dtype, msg):
        if dtype == 'counter':
            return msg.counter_value
        elif dtype == 'set':
            return self._decode_set_value(msg.set_value)
        elif dtype == 'map':
            return self._decode_map_value(msg.map_value)

    def _decode_map_value(self, entries):
        out = {}
        for entry in entries:
            name = entry.field.name[:].decode()
            dtype = codec.MAP_FIELD_TYPES[entry.field.type]
            if dtype == 'counter':
                value = entry.counter_value
            elif dtype == 'set':
                value = self._decode_set_value(entry.set_value)
            elif dtype == 'register':
                value = entry.register_value[:].decode()
            elif dtype == 'flag':
                value = entry.flag_value
            elif dtype == 'map':
                value = self._decode_map_value(entry.map_value)
            out[(name, dtype)] = value
        return out

    def _decode_set_value(self, set_value):
        return [string[:].decode() for string in set_value]

    def _decode_bucket_props(self, msg):
        '''
        Decodes the protobuf bucket properties message into a dict.
        :param msg: the protobuf message to decode
        :type msg: riak_pb.RpbBucketProps
        :rtype dict
        '''
        props = {}

        for prop_name in codec.NORMAL_PROPS:
            if msg.HasField(prop_name):
                prop = getattr(msg, prop_name)
                if isinstance(prop, bytes):
                    props[prop_name] = prop.decode()
                else:
                    props[prop_name] = prop
        for prop in codec.COMMIT_HOOK_PROPS:
            if getattr(msg, 'has_' + prop):
                props[prop] = self._decode_hooklist(getattr(msg, prop))
        for prop in codec.MODFUN_PROPS:
            if msg.HasField(prop):
                props[prop] = self._decode_modfun(getattr(msg, prop))
        for prop in codec.QUORUM_PROPS:
            if msg.HasField(prop):
                props[prop] = self._decode_quorum(getattr(msg, prop))
        if msg.HasField('repl'):
            props['repl'] = codec.REPL_TO_PY[msg.repl]

        return props

    def _decode_hooklist(self, hooklist):
        '''
        Decodes a list of protobuf commit hooks into their python
        equivalents. Used in bucket properties.
        :param hooklist: a list of protobuf commit hooks
        :type hooklist: list
        :rtype list
        '''
        return [self._decode_hook(hook) for hook in hooklist]

    def _decode_hook(self, hook):
        '''
        Decodes a protobuf commit hook message into a dict. Used in
        bucket properties.
        :param hook: the hook to decode
        :type hook: riak_pb.RpbCommitHook
        :rtype dict
        '''
        if hook.HasField('modfun'):
            return self._decode_modfun(hook.modfun)
        else:
            return {'name': hook.name}

    def _decode_modfun(self, modfun):
        '''
        Decodes a protobuf modfun pair into a dict with 'mod' and
        'fun' keys. Used in bucket properties.
        :param modfun: the protobuf message to decode
        :type modfun: riak_pb.RpbModFun
        :rtype dict
        '''
        return {'mod': modfun.module.decode(),
                'fun': modfun.function.decode()}

    def _decode_quorum(self, rw):
        '''
        Converts a protobuf quorum value to a symbolic value if
        necessary.
        :param rw: the quorum
        :type rw: int
        :rtype int or string
        '''
        if rw in codec.QUORUM_TO_PY:
            return codec.QUORUM_TO_PY[rw]
        else:
            return rw

    async def _stream(self, msg_code, msg=None, expect=None):
        self._writer.write(self._encode_message(msg_code, msg))
        self._parser = self.StreamParserClass(self._reader)
        responses = []
        async for code, pbo in self._parser:
            if expect is not None and code != expect:
                raise Exception('Unexpected response code ({})'.format(code))
            responses.append((code, pbo))
        return responses

    async def _request(self, msg_code, msg=None, expect=None):
        self._writer.write(self._encode_message(msg_code, msg))
        if self._parser:
            tail = self._parser.tail
            del self._parser
        else:
            tail = bytearray()
        self._parser = self.ParserClass(self._reader, tail)
        code, response = await self._parser.get_pbo()

        if expect is not None and code != expect:
            raise Exception('Unexpected response code ({})'.format(code))
        return code, response

    async def _read_response(self):
        while not self._reader.at_eof():
            try:
                data = await self._reader.read(MAX_CHUNK_SIZE)
            except asyncio.CancelledError:
                break
            except Exception:
                # XXX: for QUIT command connection error can be received
                #       before response
                # logger.error("Exception on data read %r", exc, exc_info=True)
                break
            self._parser.feed_data(data)
            if self._parser.at_eof():
                break
        return self._parser.msg_code, self._parser.msg

    async def ping(self, error=False):
        if error:
            _, response = await self._request(messages.MSG_CODE_PING_RESP)
        else:
            _, response = await self._request(
                messages.MSG_CODE_PING_REQ, expect=messages.MSG_CODE_PING_RESP)
        return response

    async def get_server_info(self):
        _, res = await self._request(
            messages.MSG_CODE_GET_SERVER_INFO_REQ,
            expect=messages.MSG_CODE_GET_SERVER_INFO_RESP)
        return self._decode_pbo(res)

    async def get_client_id(self):
        _, res = await self._request(
            messages.MSG_CODE_GET_CLIENT_ID_REQ,
            expect=messages.MSG_CODE_GET_CLIENT_ID_RESP)
        return self._decode_pbo(res)

    async def set_client_id(self, client_id):
        req = riak_pb.RpbSetClientIdReq()
        req.client_id = client_id

        code, res = await self._request(
            messages.MSG_CODE_SET_CLIENT_ID_REQ, req,
            expect=messages.MSG_CODE_SET_CLIENT_ID_RESP)
        if code == messages.MSG_CODE_SET_CLIENT_ID_RESP:
            return True
        else:
            return False

    async def get_bucket_type_props(self, bucket_type):
        '''
        Fetch bucket-type properties
        :param bucket_type: A :class:`BucketType <aioriak.bucket.BucketType>`
               instance
        :type bucket_type: :class:`BucketType <aioriak.bucket.BucketType>`
        '''
        req = riak_pb.RpbGetBucketTypeReq()
        req.type = bucket_type.name.encode()

        msg_code, resp = await self._request(
            messages.MSG_CODE_GET_BUCKET_TYPE_REQ, req,
            messages.MSG_CODE_GET_BUCKET_RESP)
        return self._decode_bucket_props(resp.props)

    async def fetch_datatype(self, bucket, key):

        if bucket.bucket_type.is_default():
            raise NotImplementedError("Datatypes cannot be used in the default"
                                      " bucket-type.")
        req = riak_pb.DtFetchReq()
        req.type = bucket.bucket_type.name.encode()
        req.bucket = bucket.name.encode()
        req.key = key.encode()

        msg_code, resp = await self._request(messages.MSG_CODE_DT_FETCH_REQ,
                                             req,
                                             messages.MSG_CODE_DT_FETCH_RESP)

        return self._decode_dt_fetch(resp)

    async def set_bucket_type_props(self, bucket_type, props):
        '''
        Set bucket-type properties
        :param bucket_type: A :class:`BucketType <aioriak.bucket.BucketType>`
               instance
        :type bucket_type: :class:`BucketType <aioriak.bucket.BucketType>`
        '''
        req = riak_pb.RpbSetBucketTypeReq()
        req.type = bucket_type.name.encode()

        self._encode_bucket_props(props, req)

        msg_code, resp = await self._request(
            messages.MSG_CODE_SET_BUCKET_TYPE_REQ, req,
            messages.MSG_CODE_SET_BUCKET_RESP)
        return True

    def _add_bucket_type(self, req, bucket_type):
        if bucket_type and not bucket_type.is_default():
            req.type = bucket_type.name.encode()

    async def get_buckets(self, bucket_type=None):
        req = riak_pb.RpbListBucketsReq()
        if bucket_type:
            self._add_bucket_type(req, bucket_type)
        code, res = await self._request(messages.MSG_CODE_LIST_BUCKETS_REQ,
                                        req,
                                        messages.MSG_CODE_LIST_BUCKETS_RESP)
        return res.buckets

    async def get_keys(self, bucket):
        """
        Lists all keys within a bucket.
        """
        req = riak_pb.RpbListKeysReq()
        req.bucket = bucket.name.encode()
        keys = []
        self._add_bucket_type(req, bucket.bucket_type)
        for code, res in await self._stream(messages.MSG_CODE_LIST_KEYS_REQ,
                                            req,
                                            messages.MSG_CODE_LIST_KEYS_RESP):
            for key in res.keys:
                keys.append(key.decode())
        return keys

    def _decode_contents(self, contents, obj):
        """
        Decodes the list of siblings from the protobuf representation
        into the object.
        :param contents: a list of RpbContent messages
        :type contents: list
        :param obj: a RiakObject
        :type obj: RiakObject
        :rtype RiakObject
        """
        obj.siblings = [self._decode_content(c, RiakContent(obj))
                        for c in contents]
        # Invoke sibling-resolution logic
        if len(obj.siblings) > 1 and obj.resolver is not None:
            obj.resolver(obj)
        return obj

    def _decode_content(self, rpb_content, sibling):
        """
        Decodes a single sibling from the protobuf representation into
        a RiakObject.
        :param rpb_content: a single RpbContent message
        :type rpb_content: riak_pb.RpbContent
        :param sibling: a RiakContent sibling container
        :type sibling: RiakContent
        :rtype: RiakContent
        """

        if rpb_content.HasField("deleted") and rpb_content.deleted:
            sibling.exists = False
        else:
            sibling.exists = True
        if rpb_content.HasField("content_type"):
            sibling.content_type = rpb_content.content_type.decode()
        if rpb_content.HasField("charset"):
            sibling.charset = rpb_content.charset.decode()
        if rpb_content.HasField("content_encoding"):
            sibling.content_encoding = rpb_content.content_encoding.decode()
        if rpb_content.HasField("vtag"):
            sibling.etag = rpb_content.vtag.decode()

        sibling.links = [self._decode_link(link)
                         for link in rpb_content.links]
        if rpb_content.HasField("last_mod"):
            sibling.last_modified = float(rpb_content.last_mod)
            if rpb_content.HasField("last_mod_usecs"):
                sibling.last_modified += rpb_content.last_mod_usecs / 1000000.0

        sibling.usermeta = dict([(usermd.key, usermd.value)
                                 for usermd in rpb_content.usermeta])
        sibling.indexes = set([(index.key,
                                decode_index_value(index.key, index.value))
                               for index in rpb_content.indexes])

        sibling.encoded_data = rpb_content.value

        return sibling

    def _decode_link(self, link):
        """
        Decodes an RpbLink message into a tuple
        :param link: an RpbLink message
        :type link: riak_pb.RpbLink
        :rtype tuple
        """

        if link.HasField("bucket"):
            bucket = link.bucket
        else:
            bucket = None
        if link.HasField("key"):
            key = link.key
        else:
            key = None
        if link.HasField("tag"):
            tag = link.tag
        else:
            tag = None

        return (bucket, key, tag)

    async def get(self, robj):
        '''
        Serialize get request and deserialize response
        '''
        bucket = robj.bucket

        req = riak_pb.RpbGetReq()
        req.bucket = bucket.name.encode()
        self._add_bucket_type(req, bucket.bucket_type)
        req.key = robj.key.encode()

        msg_code, resp = await self._request(messages.MSG_CODE_GET_REQ, req,
                                             messages.MSG_CODE_GET_RESP)
        if resp is not None:
            # if resp.HasField('vclock'):
            #    robj.vclock = VClock(resp.vclock, 'binary')
            # We should do this even if there are no contents, i.e.
            # the object is tombstoned
            self._decode_contents(resp.content, robj)
        else:
            # "not found" returns an empty message,
            # so let's make sure to clear the siblings
            robj.siblings = []
        return robj
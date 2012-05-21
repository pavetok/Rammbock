#  Copyright 2012 Nokia Siemens Networks Oyj
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from math import ceil
import re

from Rammbock.message import Field, Union, Message, Header, List, Struct, BinaryContainer, BinaryField, TBCDContainer
from message_stream import MessageStream
from primitives import Length, Binary, TBCD
from Rammbock.ordered_dict import OrderedDict
from Rammbock.binary_tools import to_binary_string_of_length, to_bin, to_tbcd_value, to_tbcd_binary


class _Template(object):

    def __init__(self, name, parent):
        self.parent = parent
        self._fields = OrderedDict()
        self.name = name

    def _pretty_print_fields(self, fields):
        return ', '.join('%s:%s' % (key, value) for key, value in fields.items())

    def _mark_referenced_field(self, field):
        ref_field = self._get_field_recursive(field.length.field)
        if not ref_field:
            raise AssertionError('Length field %s unknown' % field.length.field)
        ref_field.referenced_later = True

    def add(self, field):
        if field.type == 'pdu':
            self._handle_pdu_field(field)
        if self._get_field(field.name):
            raise AssertionError('Duplicate field %s in %s' % (field.name, self.name))
        if field.has_length and field.length.has_references:
            self._mark_referenced_field(field)
        self._fields[field.name] = field

    def _handle_pdu_field(self, field):
        raise AssertionError('PDU field not allowed')

    def _get_field(self, field_name):
        return self._fields.get(field_name)

    def _get_field_recursive(self, field_name):
        return self._get_field(field_name) or self.parent and self.parent._get_field_recursive(field_name)

    def _check_params_empty(self, message_fields, name):
        message_fields.pop('*', None)
        if message_fields:
            raise AssertionError('Unknown fields in %s %s' %
                                 (self._get_recursive_name(name), self._pretty_print_fields(message_fields)))

    def _get_recursive_name(self, name):
        return self.parent._get_recursive_name(name) if self.parent else name

    def _encode_fields(self, struct, params, little_endian=False):
        for field in self._fields.values():
            encoded = field.encode(params, struct, little_endian=little_endian)
            # TODO: clean away this ugly hack that makes it possible to skip PDU
            # (now it is a 0 length place holder in header)
            if encoded:
                struct[field.name] = encoded
        self._check_params_empty(params, self.name)

    def decode(self, data, parent=None, name=None, little_endian=False):
        message = self._get_struct(name, parent)
        data_index = 0
        for field in self._fields.values():
            message[field.name] = field.decode(data[data_index:], message, little_endian=little_endian)
            data_index += len(message[field.name])
        return message

    def validate(self, message, message_fields):
        errors = []
        for field in self._fields.values():
            errors += field.validate(message, message_fields)
        self._check_params_empty(message_fields, self.name)
        return errors

    def _get_params_sub_tree(self, params, name=None):
        result = {'*': params['*']} if '*' in params else {}
        name = name or self.name
        for key in params.keys():
            prefix, _, ending = key.partition('.')
            if prefix == name:
                result[ending] = params.pop(key)
        return result

    def _get_struct(self, name, parent):
        return None

#TODO: Refactor the pdu to use the same dynamic length strategy as structs in encoding
class Protocol(_Template):

    def __init__(self, name):
        _Template.__init__(self, name, None)
        self.pdu = None

    def header_length(self):
        length = 0
        for field in self._fields.values():
            if not field.length.static:
                return length
            length += field.length.value
        return length

    def encode(self, message, header_params, little_endian=False):
        header_params = header_params.copy()
        header = Header(self.name)
        self._encode_fields(header, header_params, little_endian=little_endian)
        return header

    def _handle_pdu_field(self, field):
        if self.pdu:
            raise AssertionError('Duplicate PDU field not allowed in protocol definition.')
        self.pdu = field

    @property
    def pdu_length(self):
        return self.pdu.length

    def add(self, field):
        if self.pdu:
            raise AssertionError('Fields after PDU not supported.')
        _Template.add(self, field)

    # TODO: fields after the pdu
    def _extract_values_from_data(self, data, header, values):
        data_index = 0
        field_index = 0
        while len(data) > data_index:
            field = values[field_index]
            header[field.name] = Field(field.type, field.name,
                data[data_index:data_index + field.length.value])
            data_index += field.length.value
            field_index += 1

    def read(self, stream, timeout=None):
        data = stream.read(self.header_length(), timeout=timeout)
        header = Header(self.name)
        self._extract_values_from_data(data, header, self._fields.values())
        length_param = header[self.pdu_length.field].int
        pdu_bytes = stream.read(self.pdu_length.calc_value(length_param))
        return (header, pdu_bytes)

    def get_message_stream(self, buffered_stream):
        return MessageStream(buffered_stream, self)

    def verify(self):
        if not self.pdu:
            raise AssertionError('Protocol definition must include a pdu field.')


class MessageTemplate(_Template):

    type = 'Message'

    def __init__(self, message_name, protocol, header_params):
        _Template.__init__(self, message_name, None)
        self._protocol = protocol
        self.header_parameters = header_params
        self.length = protocol.pdu_length

    def encode(self, message_params, header_params, little_endian=False):
        message_params = message_params.copy()
        msg = Message(self.name)
        self._encode_fields(msg, message_params, little_endian=little_endian)
        if self._protocol:
            # TODO: little endian support for protocol header
            header = self._protocol.encode(msg, self._headers(header_params))
            self.length.find_length_and_set_if_necessary(header, len(msg._raw))
            msg._add_header(header)
        return msg

    def _headers(self, header_params):
        result = {}
        result.update(self.header_parameters)
        result.update(header_params)
        return result

    def _get_struct(self, name, parent=None):
        return Message(self.name)


class StructTemplate(_Template):

    has_length = False
    
    def __init__(self, type, name, parent, parameters=None, length=None):
        self._parameters = parameters or {}
        self.type = type
        if length:
            self._set_length(length)
        _Template.__init__(self, name, parent)

    def _set_length(self, length):
        self.has_length = True
        self.length = Length(length)

    def get_static_length(self):
        return sum(field.get_static_length() for field in self._fields.values())

    def decode(self, data, parent=None, name=None, little_endian=False):
        if self.has_length:
            length = self.length.decode(parent)
            data = data[:length]
        return _Template.decode(self, data, parent, name, little_endian)

    def encode(self, message_params, parent=None, name=None, little_endian=False):
        struct = self._get_struct(name, parent)
        self._add_struct_params(message_params)
        self._encode_fields(struct, self._get_params_sub_tree(message_params,
            name), little_endian=little_endian)
        if self.has_length:
            length, aligned_length = self.length.find_length_and_set_if_necessary(parent, len(struct))
            if len(struct) != length:
                raise AssertionError('Length of struct %s does not match defined length. defined length:%s Struct:\n%s' % (self.name, length, repr(struct)))
        return struct

    # TODO: Cleanup setting the parent to constructor of message -elements
    def _get_struct(self, name, parent):
        struct = Struct(name or self.name, self.type)
        struct._parent = parent
        return struct

    def validate(self, parent, message_fields, name=None):
        self._add_struct_params(message_fields)
        errors = []
        name = name or self.name
        message = parent[name]
        if self.has_length:
            length = self.length.decode(message)
            if len(message) != length:
                errors.append('Length of struct %s does not match defined length. defined length:%s struct length:%s' % (message._name, length, len(message)))
        return errors + _Template.validate(self, message, self._get_params_sub_tree(message_fields, name))

    def _add_struct_params(self, params):
        for key in self._parameters.keys():
            params[key] = self._parameters.pop(key) if key not in params else params[key]


class UnionTemplate(_Template):
    
    has_length = False
    
    def __init__(self, type, name, parent):
        self.type = type
        _Template.__init__(self, name, parent)
    
    def add(self, field):
        field.get_static_length()
        self._fields[field.name] = field
            
    def get_static_length(self):
        return max(field.get_static_length() for field in self._fields.values())

    def decode(self, data, parent=None, name=None, little_endian=False):
        union = self._get_struct(name, parent)
        for field in self._fields.values():
            union[field.name] = field.decode(data, union, little_endian=little_endian)
        return union
    
    def encode(self, union_params, parent=None, name=None, little_endian=False):
        name = name or self.name
        if name not in union_params:
            raise AssertionError('Value not chosen for union %s' % name)
        chosen_one = union_params[name]
        if chosen_one not in self._fields:
            raise Exception('Unknown union field %s' % chosen_one)
        field = self._fields[chosen_one]
        union = self._get_struct(name, parent)
        union[field.name] = field.encode(self._get_params_sub_tree(union_params, name),
                                         union,
                                         little_endian=little_endian)
        return union

    def _get_struct(self, name, parent):
        union = Union(name or self.name, self.get_static_length())
        union._parent = parent
        return union

    def validate(self, parent, message_fields, name=None):
        name = name or self.name
        message = parent[name]
        return _Template.validate(self, message, self._get_params_sub_tree(message_fields, name))

#TODO: check that only one field is added to list
#TODO: list field could be overriden
class ListTemplate(_Template):

    param_pattern = re.compile(r'(.*?)\[(.*?)\](.*)')
    has_length = True
    type = 'List'

    def __init__(self, length, name, parent):
        self.length = Length(length)
        _Template.__init__(self, name, parent)

    def get_static_length(self):
        return self.length.value * self.field.get_static_length()

    def encode(self, message_params, parent, name=None, little_endian=False):
        name = name or self.name
        params_subtree = self._get_params_sub_tree(message_params, name)
        list = self._get_struct(name, parent)
        for index in range(self.length.decode(parent)):
            list[str(index)] = self.field.encode(params_subtree,
                                                 parent,
                                                 name=str(index), 
                                                 little_endian=little_endian)
        self._check_params_empty(params_subtree, name)
        return list

    @property
    def field(self):
        return self._fields.values()[0]

    def _get_struct(self, name=None, parent=None):
        ls = List(name or self.name, self.field.type)
        ls._parent = parent
        return ls

    def decode(self, data, parent, name=None, little_endian=False):
        name = name or self.name
        message = self._get_struct(name, parent)
        data_index = 0
        for index in range(0, self.length.decode(parent)):
            message[str(index)] = self.field.decode(data[data_index:], message, name=str(index), little_endian=little_endian)
            data_index += len(message[index])
        return message

    def validate(self, parent, message_fields, name=None):
        name = name or self.name
        params_subtree = self._get_params_sub_tree(message_fields, name)
        list = parent[name]
        errors = []
        for index in range(self.length.decode(parent)):
            errors += self.field.validate(list, params_subtree, name=str(index))
        self._check_params_empty(params_subtree, name)
        return errors

    def _get_params_sub_tree(self, params, name=None):
        # TODO: Test for * syntax in array subfields
        result = {'*': params['*']} if '*' in params else {}
        name = name or self.name
        for key in params.keys():
            match = self.param_pattern.match(key)
            if match:        
                prefix, child_name, ending = match.groups()
                if prefix == name or prefix == '*':
                    result[child_name + ending] =  params.pop(key)
        return result


class BinaryContainerTemplate(_Template):

    has_length = False
    type = 'BinaryContainer'

    def add(self, field):
        if not isinstance(field, Binary):
            raise AssertionError('Binary container can only have binary fields.')
        _Template.add(self, field)

    @property
    def binlength(self):
        return sum(field.length.value for field in self._fields.values())

    def verify(self):
        if self.binlength % 8:
            raise AssertionError('Length of binary container %s has to be divisible by 8. Length %s' % (self.name, self.binlength))

    def encode(self, message_params, parent=None, name=None, little_endian=False):
        container = self._get_struct(name, parent, little_endian=little_endian)
        self._encode_fields(container, self._get_params_sub_tree(message_params, name))
        return container

    def decode(self, data, parent=None, name=None, little_endian=False):
        container = self._get_struct(name, parent, little_endian=little_endian)
        if little_endian:
            data = data[::-1]
        bin_str = to_binary_string_of_length(self.binlength, data[:self.binlength/8])
        data_index = 2
        for field in self._fields.values():
            container[field.name] = BinaryField(field.length.value, field.name,
                to_bin("0b" + bin_str[data_index:data_index + field.length.value]))
            data_index += field.length.value
        return container

    def validate(self, parent, message_fields, name=None):
        name = name or self.name
        errors = []
        message = parent[name]
        return errors + _Template.validate(self, message, self._get_params_sub_tree(message_fields, name))

    def _get_struct(self, name, parent, little_endian=False):
        cont = BinaryContainer(name or self.name, little_endian=little_endian)
        cont._parent = parent
        return cont


class TBCDContainerTemplate(_Template):

    has_length = False
    type = 'TBCDContainer'

    def _verify_not_little_endian(self, little_endian):
        if little_endian:
            raise AssertionError('Little endian TBCD fields are not supported.')

    def add(self, field):
        if not isinstance(field, TBCD):
            raise AssertionError('TBCD container can only have TBCD fields.')
        _Template.add(self, field)

    def encode(self, message_params, parent=None, name=None, little_endian=False):
        self._verify_not_little_endian(little_endian)
        container = self._get_struct(name, parent)
        self._encode_fields(container, self._get_params_sub_tree(message_params, name))
        return container

    def decode(self, data, parent=None, name=None, little_endian=False):
        self._verify_not_little_endian(little_endian)
        container = self._get_struct(name, parent)
        a = to_tbcd_value(data)
        index = 0
        for field in self._fields.values():
            field_length = field.length.decode(container, len(data)*2-index)
            container[field.name] = Field(field_length, field.name, to_tbcd_binary(a[index:index + field_length]))
            index += field_length
        return container

    def validate(self, parent, message_fields, name=None):
        name = name or self.name
        errors = []
        return errors + _Template.validate(self, parent[name], self._get_params_sub_tree(message_fields, name))

    @property
    def binlength(self):
        length = sum(field.length.value for field in self._fields.values())
        return int(ceil(length / 2.0) * 8)

    def _get_struct(self, name, parent):
        tbcd = TBCDContainer(name or self.name)
        tbcd._parent = parent
        return tbcd

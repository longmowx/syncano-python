import json
from copy import deepcopy
from functools import wraps

import six

from syncano.connection import ConnectionMixin
from syncano.exceptions import SyncanoRequestError, SyncanoValueError

from .registry import registry

# The maximum number of items to display in a Manager.__repr__
REPR_OUTPUT_SIZE = 20


def clone(func):
    """Decorator which will ensure that we are working on copy of ``self``."""

    @wraps(func)
    def inner(self, *args, **kwargs):
        self = self._clone()
        return func(self, *args, **kwargs)
    return inner


class ManagerDescriptor(object):

    def __init__(self, manager):
        self.manager = manager

    def __get__(self, instance, owner=None):
        if instance is not None:
            raise AttributeError("Manager isn't accessible via {0} instances.".format(owner.__name__))
        return self.manager.all()


class RelatedManagerDescriptor(object):

    def __init__(self, field, name, endpoint):
        self.field = field
        self.name = name
        self.endpoint = endpoint

    def __get__(self, instance, owner=None):
        if instance is None:
            raise AttributeError("RelatedManager is accessible only via {0} instances.".format(owner.__name__))

        links = getattr(instance, self.field.name)

        if not links:
            return None

        path = links[self.name]

        if not path:
            return None

        Model = registry.get_model_by_path(path)
        method = getattr(Model.please, self.endpoint, Model.please.all)

        properties = instance._meta.get_endpoint_properties('detail')
        properties = [getattr(instance, prop) for prop in properties]

        return method(*properties)


class Manager(ConnectionMixin):
    """Base class responsible for all ORM (``please``) actions."""

    def __init__(self):
        self.name = None
        self.model = None

        self.endpoint = None
        self.properties = {}

        self.method = None
        self.query = {}
        self.data = {}

        self._limit = None
        self._serialize = True
        self._connection = None

    def __repr__(self):  # pragma: no cover
        data = list(self[:REPR_OUTPUT_SIZE + 1])
        if len(data) > REPR_OUTPUT_SIZE:
            data[-1] = '...(remaining elements truncated)...'
        return repr(data)

    def __str__(self):  # pragma: no cover
        return '<Manager: {0}>'.format(self.model.__name__)

    def __unicode__(self):  # pragma: no cover
        return six.u(str(self))

    def __len__(self):  # pragma: no cover
        return self.iterator()

    def __iter__(self):  # pragma: no cover
        return iter(self.iterator())

    def __bool__(self):  # pragma: no cover
        return bool(self.iterator())

    def __nonzero__(self):  # pragma: no cover
        return type(self).__bool__(self)

    def __getitem__(self, k):
        """
        Retrieves an item or slice from the set of results.
        """
        if not isinstance(k, (slice,) + six.integer_types):
            raise TypeError
        assert ((not isinstance(k, slice) and (k >= 0)) or
                (isinstance(k, slice) and (k.start is None or k.start >= 0) and
                 (k.stop is None or k.stop >= 0))), \
            "Negative indexing is not supported."

        manager = self._clone()

        if isinstance(k, slice):
            if k.stop is not None:
                manager.limit(int(k.stop) + 1)
            return list(manager)[k.start:k.stop:k.step]

        manager.limit(k + 1)
        return list(manager)[k]

    # Object actions

    def create(self, **kwargs):
        """
        A convenience method for creating an object and saving it all in one step. Thus::

            instance = Instance.please.create(name='test-one', description='description')

        and::

            instance = Instance(name='test-one', description='description')
            instance.save()

        are equivalent.
        """
        attrs = kwargs.copy()
        attrs.update(self.properties)

        instance = self.model(**attrs)
        instance.save()

        return instance

    def bulk_create(self, *objects):
        """
        Creates many new instances based on provided list of objects.

        Usage::

            objects = [{'name': 'test-one'}, {'name': 'test-two'}]
            instances = Instance.please.bulk_create(objects)

        .. warning::
            This method is not meant to be used with large data sets.
        """
        return [self.create(**o) for o in objects]

    @clone
    def get(self, *args, **kwargs):
        """
        Returns the object matching the given lookup parameters.

        Usage::

            instance = Instance.please.get('test-one')
            instance = Instance.please.get(name='test-one')
        """
        self.method = 'GET'
        self.endpoint = 'detail'
        self._filter(*args, **kwargs)
        return self.request()

    def detail(self, *args, **kwargs):
        """
        Wrapper around ``get`` method.

        Usage::

            instance = Instance.please.detail('test-one')
            instance = Instance.please.detail(name='test-one')
        """
        return self.get(*args, **kwargs)

    def get_or_create(self, **kwargs):
        """
        A convenience method for looking up an object with the given
        lookup parameters, creating one if necessary.

        Returns a tuple of **(object, created)**, where **object** is the retrieved or
        **created** object and created is a boolean specifying whether a new object was created.

        This is meant as a shortcut to boilerplatish code. For example::

            try:
                instance = Instance.please.get(name='test-one')
            except Instance.DoesNotExist:
                instance = Instance(name='test-one', description='test')
                instance.save()

        The above example can be rewritten using **get_or_create()** like so::

            instance, created = Instance.please.get_or_create(name='test-one', defaults={'description': 'test'})
        """
        defaults = deepcopy(kwargs.pop('defaults', {}))
        try:
            instance = self.get(**kwargs)
        except self.model.DoesNotExist:
            defaults.update(kwargs)
            instance = self.create(**defaults)
            created = True
        else:
            created = False
        return instance, created

    @clone
    def delete(self, *args, **kwargs):
        """
        Removes single instance based on provided arguments.

        Usage::

            instance = Instance.please.delete('test-one')
            instance = Instance.please.delete(name='test-one')
        """
        self.method = 'DELETE'
        self.endpoint = 'detail'
        self._filter(*args, **kwargs)
        return self.request()

    @clone
    def update(self, *args, **kwargs):
        """
        Updates single instance based on provided arguments.
        The **data** is a dictionary of (field, value) pairs used to update the object.

        Usage::

            instance = Instance.please.update('test-one', data={'description': 'new one'})
            instance = Instance.please.update(name='test-one', data={'description': 'new one'})
        """
        self.endpoint = 'detail'
        self.method = self.get_allowed_method('PUT', 'PATCH', 'POST')
        self.data = kwargs.pop('data')
        self._filter(*args, **kwargs)
        return self.request()

    def update_or_create(self, defaults=None, **kwargs):
        """
        A convenience method for updating an object with the given parameters, creating a new one if necessary.
        The ``defaults`` is a dictionary of (field, value) pairs used to update the object.

        Returns a tuple of **(object, created)**, where object is the created or updated object and created
        is a boolean specifying whether a new object was created.

        The **update_or_create** method tries to fetch an object from Syncano API based on the given kwargs.
        If a match is found, it updates the fields passed in the defaults dictionary.

        This is meant as a shortcut to boilerplatish code. For example::

            try:
                instance = Instance.please.update(name='test-one', data=updated_values)
            except Instance.DoesNotExist:
                updated_values.update({'name': 'test-one'})
                instance = Instance(**updated_values)
                instance.save()

        This pattern gets quite unwieldy as the number of fields in a model goes up.
        The above example can be rewritten using **update_or_create()** like so::

            instance, created = Instance.please.update_or_create(name='test-one',
                                                                 defaults=updated_values)
        """
        defaults = deepcopy(defaults or {})
        try:
            instance = self.update(**kwargs)
        except self.model.DoesNotExist:
            defaults.update(kwargs)
            instance = self.create(**defaults)
            created = True
        else:
            created = False
        return instance, created

    # List actions

    @clone
    def all(self, *args, **kwargs):
        """
        Returns a copy of the current ``Manager`` with limit removed.

        Usage::

            instances = Instance.please.all()
        """
        self._limit = None
        return self.list(*args, **kwargs)

    @clone
    def list(self, *args, **kwargs):
        """
        Returns a copy of the current ``Manager`` containing objects that match the given lookup parameters.

        Usage::
            instance = Instance.please.list()
            classes = Class.please.list(instance_name='test-one')
        """
        self.method = 'GET'
        self.endpoint = 'list'
        self._filter(*args, **kwargs)
        return self

    @clone
    def first(self, *args, **kwargs):
        """
        Returns the first object matched by the lookup parameters or None, if there is no matching object.

        Usage::

            instance = Instance.please.first()
            classes = Class.please.first(instance_name='test-one')
        """
        try:
            self._limit = 1
            return self.list(*args, **kwargs)[0]
        except KeyError:
            return None

    @clone
    def page_size(self, value):
        """
        Sets page size.

        Usage::

            instances = Instance.please.page_size(20).all()
        """
        if not value or not isinstance(value, six.integer_types):
            raise SyncanoValueError('page_size value needs to be an int.')

        self.query['page_size'] = value
        return self

    @clone
    def limit(self, value):
        """
        Sets limit of returned objects.

        Usage::

            instances = Instance.please.list().limit(10)
            classes = Class.please.list(instance_name='test-one').limit(10)
        """
        if not value or not isinstance(value, six.integer_types):
            raise SyncanoValueError('Limit value needs to be an int.')

        self._limit = value
        return self

    @clone
    def order_by(self, field):
        """
        Sets order of returned objects.

        Usage::

            instances = Instance.please.order_by('name')
        """
        if not field or not isinstance(field, six.string_types):
            raise SyncanoValueError('Order by field needs to be a string.')

        self.query['order_by'] = field
        return self

    @clone
    def raw(self):
        """
        Disables serialization. ``request`` method will return raw Python types.

        Usage::

            >>> instances = Instance.please.list().raw()
            >>> instances
            [{'description': 'new one', 'name': 'test-one'...}...]
        """
        self._serialize = False
        return self

    @clone
    def using(self, connection):
        """
        Connection juggling.
        """
        # ConnectionMixin will validate this
        self.connection = connection
        return self

    # Other stuff

    def contribute_to_class(self, model, name):  # pragma: no cover
        setattr(model, name, ManagerDescriptor(self))

        self.model = model

        if not self.name:
            self.name = name

    def _filter(self, *args, **kwargs):
        if args and self.endpoint:
            properties = self.model._meta.get_endpoint_properties(self.endpoint)
            mapped_args = {k: v for k, v in zip(properties, args)}
            self.properties.update(mapped_args)
        self.properties.update(kwargs)

    def _clone(self):
        # Maybe deepcopy ?
        manager = self.__class__()
        manager.name = self.name
        manager.model = self.model
        manager._connection = self._connection
        manager.endpoint = self.endpoint
        manager.properties = deepcopy(self.properties)
        manager._limit = self._limit
        manager.method = self.method
        manager.query = deepcopy(self.query)
        manager.data = deepcopy(self.data)
        manager._serialize = self._serialize

        return manager

    def serialize(self, data, model=None):
        """Serializes passed data to related :class:`~syncano.models.base.Model` class."""
        model = model or self.model

        if isinstance(data, model):
            return data

        if not isinstance(data, dict):
            raise SyncanoValueError('Unsupported data type.')

        properties = deepcopy(self.properties)
        properties.update(data)

        return model(**properties) if self._serialize else data

    def request(self, method=None, path=None, **request):
        """Internal method, which calls Syncano API and returns serialized data."""
        meta = self.model._meta
        method = method or self.method
        allowed_methods = meta.get_endpoint_methods(self.endpoint)
        path = path or meta.resolve_endpoint(self.endpoint, self.properties)

        if method.lower() not in allowed_methods:
            methods = ', '.join(allowed_methods)
            raise SyncanoValueError('Unsupported request method "{0}" allowed are {1}.'.format(method, methods))

        if 'params' not in request and self.query:
            request['params'] = self.query

        if 'data' not in request and self.data:
            request['data'] = self.data

        try:
            response = self.connection.request(method, path, **request)
        except SyncanoRequestError as e:
            if e.status_code == 404:
                raise self.model.DoesNotExist
            raise

        if 'next' not in response:
            return self.serialize(response)

        return response

    def get_allowed_method(self, *methods):
        meta = self.model._meta
        allowed_methods = meta.get_endpoint_methods(self.endpoint)

        for method in methods:
            if method.lower() in allowed_methods:
                return method

        methods = ', '.join(methods)
        raise SyncanoValueError('Unsupported request methods {0}.'.format(methods))

    def iterator(self):
        """Pagination handler"""

        response = self.request()
        results = 0
        while True:
            objects = response.get('objects')
            next_url = response.get('next')

            for o in objects:
                if self._limit and results >= self._limit:
                    break

                results += 1
                yield self.serialize(o)

            if not objects or not next_url or (self._limit and results >= self._limit):
                break

            response = self.request(path=next_url)


class CodeBoxManager(Manager):
    """
    Custom :class:`~syncano.models.manager.Manager`
    class for :class:`~syncano.models.base.CodeBox` model.
    """

    @clone
    def run(self, *args, **kwargs):
        payload = kwargs.pop('payload', {})

        if not isinstance(payload, six.string_types):
            payload = json.dumps(payload)

        self.method = 'POST'
        self.endpoint = 'run'
        self.data['payload'] = payload
        self._filter(*args, **kwargs)
        self._serialize = False
        response = self.request()
        return registry.CodeBoxTrace(**response)


class WebhookManager(Manager):
    """
    Custom :class:`~syncano.models.manager.Manager`
    class for :class:`~syncano.models.base.Webhook` model.
    """

    @clone
    def run(self, *args, **kwargs):
        payload = kwargs.pop('payload', {})

        if not isinstance(payload, six.string_types):
            payload = json.dumps(payload)

        self.method = 'POST'
        self.endpoint = 'run'
        self.data['payload'] = payload
        self._filter(*args, **kwargs)
        self._serialize = False
        response = self.request()

        # Workaround for circular import
        return registry.Webhook.RESULT_CLASS(**response)


class ObjectManager(Manager):
    """
    Custom :class:`~syncano.models.manager.Manager`
    class for :class:`~syncano.models.base.Object` model.
    """
    LOOKUP_SEPARATOR = '__'
    ALLOWED_LOOKUPS = [
        'gt', 'gte', 'lt', 'lte',
        'eq', 'neq', 'exists', 'in',
    ]

    def create(self, **kwargs):
        attrs = kwargs.copy()
        attrs.update(self.properties)

        model = self.model.get_subclass_model(**attrs)
        instance = model(**attrs)
        instance.save()

        return instance

    def serialize(self, data, model=None):
        model = self.model.get_subclass_model(**self.properties)
        return super(ObjectManager, self).serialize(data, model)

    @clone
    def filter(self, **kwargs):
        """
        Special method just for data object :class:`~syncano.models.base.Object` model.

        Usage::

            objects = Object.please.list('instance-name', 'class-name').filter(henryk__gte='hello')
        """
        query = {}
        model = self.model.get_subclass_model(**self.properties)

        for field_name, value in six.iteritems(kwargs):
            lookup = 'eq'

            if self.LOOKUP_SEPARATOR in field_name:
                field_name, lookup = field_name.split(self.LOOKUP_SEPARATOR, 1)

            if field_name not in model._meta.field_names:
                allowed = ', '.join(model._meta.field_names)
                raise SyncanoValueError('Invalid field name "{0}" allowed are {1}.'.format(field_name, allowed))

            if lookup not in self.ALLOWED_LOOKUPS:
                allowed = ', '.join(self.ALLOWED_LOOKUPS)
                raise SyncanoValueError('Invalid lookup type "{0}" allowed are {1}.'.format(lookup, allowed))

            for field in model._meta.fields:
                if field.name == field_name:
                    break

            query.setdefault(field_name, {})
            query[field_name]['_{0}'.format(lookup)] = field.to_query(value, lookup)

        self.query['query'] = json.dumps(query)
        self.method = 'GET'
        self.endpoint = 'list'
        return self


class SchemaManager(object):
    """
    Custom :class:`~syncano.models.manager.Manager`
    class for :class:`~syncano.models.fields.SchemaFiled`.
    """

    def __init__(self, schema=None):
        self.schema = schema or []

    def __eq__(self, other):
        if isinstance(other, SchemaManager):
            return self.schema == other.schema
        return NotImplemented

    def __str__(self):  # pragma: no cover
        return str(self.schema)

    def __repr__(self):  # pragma: no cover
        return '<SchemaManager>'

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.schema[key]

        if isinstance(key, six.string_types):
            for v in self.schema:
                if v['name'] == key:
                    return v

        raise KeyError

    def __setitem__(self, key, value):
        value = deepcopy(value)
        value['name'] = key
        self.remove(key)
        self.add(value)

    def __delitem__(self, key):
        self.remove(key)

    def __iter__(self):
        return iter(self.schema)

    def __contains__(self, item):
        if not self.schema:
            return False
        return item in self.schema

    def set(self, value):
        """Sets schema value."""
        self.schema = value

    def add(self, *objects):
        """Adds multiple objects to schema."""
        self.schema.extend(objects)

    def remove(self, *names):
        """Removes selected objects based on their names."""
        values = [v for v in self.schema if v['name'] not in names]
        self.set(values)

    def clear(self):
        """Sets empty schema."""
        self.set([])

    def set_index(self, field, order=False, filter=False):
        """Sets index on selected field.

        :type field: string
        :param field: Name of schema field

        :type filter: bool
        :param filter: Sets filter index on selected field

        :type order: bool
        :param order: Sets order index on selected field
        """
        if not order and not filter:
            raise ValueError('Choose at least one index.')

        if order:
            self[field]['order_index'] = True

        if filter:
            self[field]['filter_index'] = True

    def set_order_index(self, field):
        """Shortcut for ``set_index(field, order=True)``."""
        self.set_index(field, order=True)

    def set_filter_index(self, field):
        """Shortcut for ``set_index(field, filter=True)``."""
        self.set_index(field, filter=True)

    def remove_index(self, field, order=False, filter=False):
        """Removes index from selected field.

        :type field: string
        :param field: Name of schema field

        :type filter: bool
        :param filter: Removes filter index from selected field

        :type order: bool
        :param order: Removes order index from selected field
        """
        if not order and not filter:
            raise ValueError('Choose at least one index.')

        if order and 'order_index' in self[field]:
            del self[field]['order_index']

        if filter and 'filter_index' in self[field]:
            del self[field]['filter_index']

    def remove_order_index(self, field):
        """Shortcut for ``remove_index(field, order=True)``."""
        self.remove_index(field, order=True)

    def remove_filter_index(self, field):
        """Shortcut for ``remove_index(field, filter=True)``."""
        self.remove_index(field, filter=True)

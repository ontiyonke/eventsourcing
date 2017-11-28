import hashlib
import json
from abc import abstractmethod
from collections import deque

import os

from eventsourcing.domain.model.entity import TimestampedVersionedEntity, WithReflexiveMutator
from eventsourcing.domain.model.events import publish
from eventsourcing.exceptions import MismatchedLastHashError, SealHashMismatch
from eventsourcing.utils.transcoding import ObjectJSONEncoder

GENESIS_HASH = os.getenv('EVENTSOURCING_GENESIS_HASH', '')


class AggregateRoot(WithReflexiveMutator, TimestampedVersionedEntity):
    """
    Root entity for an aggregate in a domain driven design.
    """

    class Event(TimestampedVersionedEntity.Event):
        """Supertype for aggregate events."""
        json_encoder_class = ObjectJSONEncoder

        def __init__(self, **kwargs):
            super(AggregateRoot.Event, self).__init__(**kwargs)
            assert 'originator_hash' in self.__dict__
            # Seal the event state.
            assert 'event_hash' not in self.__dict__
            self.__dict__['event_hash'] = self.hash('sha256', self.__dict__)

        @property
        def originator_hash(self):
            return self.__dict__['originator_hash']

        @property
        def event_hash(self):
            return self.__dict__['event_hash']

        def validate(self):
            state = self.__dict__.copy()
            seal_hash = state.pop('event_hash')
            if seal_hash != self.hash('sha256', state):
                raise SealHashMismatch(self.originator_id)

        @classmethod
        def hash(cls, algorithm, *args):
            json_dump = json.dumps(
                args,
                separators=(',', ':'),
                sort_keys=True,
                cls=cls.json_encoder_class,
            )
            if algorithm == 'sha256':
                return hashlib.sha256(json_dump.encode()).hexdigest()
            else:
                raise ValueError('Algorithm not supported: {}'.format(algorithm))

        @abstractmethod
        def mutate(self, aggregate):
            aggregate.validate_event(self)
            aggregate.__head__ = self.event_hash
            aggregate.increment_version()
            aggregate.set_last_modified(self.timestamp)

    class Created(Event, TimestampedVersionedEntity.Created):
        """Published when an AggregateRoot is created."""

        def __init__(self, **kwargs):
            assert 'originator_hash' not in kwargs
            kwargs['originator_hash'] = GENESIS_HASH
            super(AggregateRoot.Created, self).__init__(**kwargs)

        def mutate(self, cls):
            aggregate = cls(**self.constructor_kwargs())
            super(AggregateRoot.Created, self).mutate(aggregate)
            return aggregate

        def constructor_kwargs(self):
            kwargs = self.__dict__.copy()
            kwargs['id'] = kwargs.pop('originator_id')
            kwargs['version'] = kwargs.pop('originator_version')
            kwargs.pop('event_hash')
            kwargs.pop('originator_hash')
            return kwargs

    class AttributeChanged(Event, TimestampedVersionedEntity.AttributeChanged):
        """Published when an AggregateRoot is changed."""

        def mutate(self, aggregate):
            super(AggregateRoot.AttributeChanged, self).mutate(aggregate)
            setattr(aggregate, self.name, self.value)
            return aggregate

    class Discarded(Event, TimestampedVersionedEntity.Discarded):
        """Published when an AggregateRoot is discarded."""

        def mutate(self, aggregate):
            super(AggregateRoot.Discarded, self).mutate(aggregate)
            assert isinstance(aggregate, AggregateRoot)
            aggregate.set_is_discarded()
            return None

    def __init__(self, **kwargs):
        super(AggregateRoot, self).__init__(**kwargs)
        self.__pending_events__ = deque()
        self.__head__ = GENESIS_HASH

    def save(self):
        """
        Publishes pending events for others in application.
        """
        batch_of_events = []
        try:
            while True:
                batch_of_events.append(self.__pending_events__.popleft())
        except IndexError:
            pass
        if batch_of_events:
            publish(batch_of_events)

    def _trigger(self, event_class, **kwargs):
        """
        Triggers domain event of given class with originator_hash as current __head__.
        """
        kwargs['originator_hash'] = self.__head__
        return super(AggregateRoot, self)._trigger(event_class, **kwargs)

    def _publish(self, event):
        """
        Appends event to internal collection of pending events.
        """
        self.__pending_events__.append(event)

    def validate_event(self, event):
        """
        Checks a domain event against the aggregate.
        """
        self._validate_last_hash(event)
        event.validate()
        self._validate_originator(event)

    def _validate_last_hash(self, event):
        """
        Checks the head hash matches the event's last hash.
        """
        if self.__head__ != event.originator_hash:
            raise MismatchedLastHashError(self.__head__, event.originator_hash)

    def increment_version(self):
        self._increment_version()

    def set_last_modified(self, last_modified):
        self._last_modified = last_modified

    def set_is_discarded(self):
        self._is_discarded = True

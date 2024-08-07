import json
import base64

from channels.generic.websocket import WebsocketConsumer
from asgiref.sync import async_to_sync
from django.core.files.base import ContentFile
from django.db.models import Q, Exists, OuterRef
from django.db.models.functions import Coalesce
from .serializers import (
    UserSerializer, 
    SearchSerializer, 
    RequestSerializer, 
    FriendSerializer, 
    MessageSerializer
)
from .models import User, Connection, Message

class ChatConsumer(WebsocketConsumer):
    def connect(self):
        user = self.scope['user']
        print(user, user.is_authenticated)
        
        if not user.is_authenticated:
            return
        
        self.username = user.username

        async_to_sync(self.channel_layer.group_add) (
            self.username, self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        async_to_sync(self.channel_layer.group_discard) (
            self.username, self.channel_name
        )

    def receive(self, text_data):
        data = json.loads(text_data)
        data_source = data.get('source')

        print('receive', json.dumps(data, indent=2))

        if data_source == 'friend.list':
            self.receive_friend_list(data)
        elif data_source == 'message.list':
            self.receive_message_list(data)
        elif data_source == 'message.send':
            self.receive_message_send(data)
        elif data_source == 'message.type':
            self.receive_message_type(data)
        elif data_source == 'request.accept':
            self.receive_request_accept(data)
        elif data_source == 'request.connect':
            self.receive_request_connect(data)
        elif data_source == 'request.list':
            self.receive_request_list(data)
        elif data_source == 'search':
            self.receive_search(data)
        elif data_source == 'thumbnail':
            self.receive_thumbnail(data)

    def receive_friend_list(self, data):
        user = self.scope['user']
        latest_message = Message.objects.filter(
            connection=OuterRef('id')
        ).order_by('-created')[:1]

        connection = Connection.objects.filter(
            Q(sender=user) | Q(receiver=user),
            accepted=True
        ).annotate(
            latest_text=latest_message.values('text'),
            latest_created=latest_message.values('created')
        ).order_by(
            Coalesce('latest_created', 'updated').desc()
        )

        serialized = FriendSerializer(
            connection, 
            context={ 'user': user }, 
            many=True
        )
        self.send_group(user.username, 'friend.list', serialized.data)

    def receive_message_list(self, data):
        user = self.scope['user']
        connectionId = data.get('connectionId')
        page = data.get('page')
        page_size = 10

        try:
            connection = Connection.objects.get(id=connectionId)
        except Connection.DoesNotExist:
            print('Error: connection not found')
            return
        
        messages = Message.objects.filter(
            connection=connection
        ).order_by('-created')[page * page_size:(page + 1) * page_size]
        serialized_message = MessageSerializer(
            messages, 
            context={'user': user},  
            many=True
        )
        
        recipient = connection.sender

        if connection.sender == user:
            recipient = connection.receiver
        
        serialized_friend = UserSerializer(recipient)

        messages_count = Message.objects.filter(
            connection=connection
        ).count()
        
        next_page = page + 1 if messages_count > (page + 1) * page_size else None

        data = {
            'messages': serialized_message.data,
            'next': next_page,
            'friend': serialized_friend.data
        } 

        self.send_group(user.username, 'message.list', data)

    def receive_message_send(self, data):
        user = self.scope['user']
        connectionId = data.get('connectionId')
        message_text = data.get('message')

        try:
            connection = Connection.objects.get(id=connectionId)
        except Connection.DoesNotExist:
            print('Error: connection not found')
            return

        message = Message.objects.create(
            connection=connection,
            user=user,
            text=message_text
        )

        recipient = connection.sender

        if connection.sender == user:
            recipient = connection.receiver

        serialized_message = MessageSerializer(
            message,
            context={'user': user}
        )
        serialized_friend = UserSerializer(user)
        data = {
            'message': serialized_message.data,
            'friend': serialized_friend.data
        }
        
        self.send_group(user.username, 'message.send', data)

        serialized_message = MessageSerializer(
            message,
            context={'user': recipient}
        )
        serialized_friend = UserSerializer(recipient)    
        data = {
            'message': serialized_message.data,
            'friend': serialized_friend.data
        }
        
        self.send_group(recipient.username, 'message.send', data)

    def receive_message_type(self, data):
        user = self.scope['user']
        recipient_username = data.get('username')
        data = {
            'username': user.username,
        }

        self.send_group(recipient_username, 'message.type', data)

    def receive_request_accept(self, data):
        username = data.get('username')

        try:
            connection = Connection.objects.get(
                sender__username=username,
                receiver=self.scope['user']
            )
        except Connection.DoesNotExist:
            print('Error: connection not found')
            return

        connection.accepted = True
        connection.save()

        serialized = RequestSerializer(connection)
        self.send_group(connection.sender.username, 'request.accept', serialized.data)
        self.send_group(connection.receiver.username, 'request.accept', serialized.data)

        serialized_friend = FriendSerializer(
            connection, 
            context={'user': connection.sender}
        )

        self.send_group(connection.sender.username, 'friend.new', serialized_friend.data)

        serialized_friend = FriendSerializer(
            connection, 
            context={'user': connection.receiver}
        )

        self.send_group(connection.receiver.username, 'friend.new', serialized_friend.data)

    def receive_request_connect(self, data):
        username = data.get('username')

        try:
            receiver = User.objects.get(username=username)
        except User.DoesNotExist:
            print('User not found')
            return

        connection, _ = Connection.objects.get_or_create(
            sender=self.scope['user'],
            receiver=receiver
        )
        serialized = RequestSerializer(connection)
        
        self.send_group(connection.sender.username, 'request.connect', serialized.data)
        self.send_group(connection.receiver.username, 'request.connect', serialized.data)

    def receive_request_list(self, data):
        user = self.scope['user']

        connections = Connection.objects.filter(
            receiver=user,
            accepted=False
        )
        serialized = RequestSerializer(connections, many=True)

        self.send_group(user.username, 'request.list', serialized.data)

    def receive_search(self, data):
        query = data.get('query')
        users = User.objects.filter(
            Q(username__istartswith=query) |
            Q(first_name__istartswith=query) |
            Q(last_name__istartswith=query)
        ).exclude(
            username=self.username
        ).annotate(
            pending_them=Exists(
                Connection.objects.filter(
                    sender=self.scope['user'],
                    receiver=OuterRef('id'),
                    accepted=False
                )
            ),
            pending_me=Exists(
                Connection.objects.filter(
                    sender=OuterRef('id'),
                    receiver=self.scope['user'],
                    accepted=False
                )
            ),
            connected=Exists(
                Connection.objects.filter(
                    Q(sender=self.scope['user'], receiver=OuterRef('id')) |
                    Q(receiver=self.scope['user'], sender=OuterRef('id')),
                    accepted=True
                )
            ),
        )

        serialized = SearchSerializer(users, many=True)
        self.send_group(self.username, 'search', serialized.data)

    def receive_thumbnail(self, data):
        user = self.scope['user']
        image_str = data.get('base64')
        image = ContentFile(base64.b64decode(image_str))
        filename = data.get('filename')

        user.thumbnail.save(filename, image, save=True)
        serialized = UserSerializer(user)
        self.send_group(self.username, 'thumbnail', serialized.data)

    def send_group(self, group, source, data):
        response = {
            'type': 'broadcast_group',
            'source': source,
            'data': data
        }
        async_to_sync(self.channel_layer.group_send) (
            group, response
        )

    def broadcast_group(self, data):
        data.pop('type')
        self.send(text_data=json.dumps(data))
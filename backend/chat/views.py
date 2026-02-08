from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(['GET'])
def room_meta(request):
    return Response({'title': '章鱼聊天室'})


from django.contrib.auth import authenticate
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from django.contrib.auth import login

User = get_user_model()

#注册
@api_view(['POST'])
def register(request):
    username = request.data.get('username')
    password = request.data.get('password')

    if User.objects.filter(username=username).exists():
        return Response({'msg': '用户已存在'}, status=400)

    user = User.objects.create_user(
        username=username,
        password=password
    )
    return Response({'msg': '注册成功'})


#登录
@api_view(['POST'])
def login_view(request):
    username = request.data.get('username')
    password = request.data.get('password')

    user = authenticate(username=username, password=password)
    if not user:
        return Response({'msg': '账号或密码错误'}, status=400)

    login(request, user)
    return Response({'msg': '登录成功'})

#校验
@api_view(['GET'])
def profile(request):
    if not request.user.is_authenticated:
        return Response({'msg': '未登录'}, status=401)

    return Response({
        'username': request.user.username
    })



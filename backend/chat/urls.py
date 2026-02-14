from django.urls import path
from .views import room_meta, room_history, room_history_days, upload_chat_image

urlpatterns = [
    path('meta/', room_meta),
    path('history/', room_history),
    path('history-days/', room_history_days),
    path('upload-image/', upload_chat_image),
]

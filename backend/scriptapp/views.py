from rest_framework.response import Response
from rest_framework.views import APIView


class ScriptHealthAPIView(APIView):
    """
    Script.app entry placeholder.
    Future script-analysis APIs should live under this app.
    """

    def get(self, request):
        return Response({"msg": "script.app ready"})

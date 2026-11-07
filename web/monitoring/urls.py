from django.urls import path

from monitoring import views

app_name = 'monitoring'

urlpatterns = [
    path('', views.welcome, name='welcome'),
    path('preparar/', views.prepare, name='prepare'),
    path('sesion/iniciar/', views.start, name='start'),
    path('sesion/', views.session, name='session'),
    path('sesion/estado/', views.state, name='state'),
    path('sesion/video/', views.video, name='video'),
    path('sesion/comando/', views.command, name='command'),
    path('sesion/finalizar/', views.finish, name='finish'),
    path('resumen/', views.summary, name='summary'),
    path('resumen/csv/', views.summary_csv, name='summary_csv'),
]

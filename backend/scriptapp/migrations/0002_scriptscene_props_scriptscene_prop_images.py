from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scriptapp", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="scriptscene",
            name="prop_images",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="scriptscene",
            name="props",
            field=models.JSONField(blank=True, default=list),
        ),
    ]

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("scriptapp", "0002_scriptscene_props_scriptscene_prop_images"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="script",
            constraint=models.UniqueConstraint(
                condition=Q(user__isnull=False),
                fields=("user", "title"),
                name="uniq_script_title_per_user",
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("books", "0002_book_cover"),
    ]

    operations = [
        migrations.AddField(
            model_name="book",
            name="bookmark_page",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
